from __future__ import annotations

import json
from typing import Any, Callable, Dict, List


def resolve_tool_timeout_arg(
    args: Dict[str, Any],
    *,
    config_key: str = "_tool_timeout_sec",
) -> float | None:
    """Resolve tool subprocess timeout from decoded tool arguments.

    Priority matches the runner helper: LLM/tool-call ``timeout_sec`` first,
    then the runner-injected config/default timeout under ``config_key``.
    Invalid or non-positive values are treated as absent, so callers get a
    single, consistent interpretation across tool implementations.
    """
    for candidate in (args.get('timeout_sec'), args.get(config_key)):
        if candidate is None:
            continue
        try:
            value = float(candidate)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None



class ToolRegistry:
    """Simple registry for OpenAI function-call compatible tools.

    - tools_spec() returns the JSON schema list to pass to the LLM
    - execute(name, arguments) dispatches to the registered callable
    """

    def __init__(self):
        self._tools: Dict[str, Dict[str, Any]] = {}

    def register(self, name: str, description: str, parameters_schema: Dict[str, Any], impl: Callable[[Dict[str, Any]], Any], local_only: bool = False):
        """Register a tool.

        Args:
            name: Tool name used in tool_calls.
            description: Human-readable description for the LLM.
            parameters_schema: JSONSchema for the tool arguments.
            impl: Callable implementing the tool. Receives a dict of args.
            local_only: If True, the tool is *not* exposed to the LLM via
                tools_spec(), but can still be executed via execute(). This is
                useful for UI-only helpers like spawn_agent or wait that should
                not be called directly by the model.
        """
        self._tools[name] = {
            "spec": {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": parameters_schema,
                }
            },
            "impl": impl,
            "local_only": local_only,
        }

    def tools_spec(self) -> List[Dict[str, Any]]:
        """Return the list of tool specs to expose to the LLM.

        Tools marked local_only=True are omitted so they can be used by the
        UI (RA3 user commands, etc.) without being surfaced as model tools.
        """
        return [d["spec"] for d in self._tools.values() if not d.get("local_only")]

    def execute(self, name: str, arguments: Any, **context: Any) -> Any:
        entry = self._tools.get(name)
        if not entry:
            raise KeyError(f"Unknown tool: {name}")
        impl = entry["impl"]
        if isinstance(arguments, str):
            try:
                args = json.loads(arguments) if arguments.strip() else {}
            except Exception:
                args = {"_raw": arguments}
        elif isinstance(arguments, dict):
            # Work on a shallow copy so we can safely inject context
            # keys (e.g. _thread_id) without mutating the caller's dict.
            args = dict(arguments)
        else:
            args = {"_arg": arguments}

        # Inject the current thread id into arguments for tools that need
        # to know "who called me" (e.g. spawn_agent). We use a reserved
        # key name to avoid colliding with user-provided arguments.
        thread_id = context.get("thread_id")
        if thread_id and "parent_thread_id" not in args and "_thread_id" not in args:
            args["_thread_id"] = thread_id

        # Similarly, propagate the calling thread's model key so other
        # tools can inherit or inspect it when needed. Spawn tools no
        # longer use this implicit override for model selection; child
        # threads should inherit from their parent thread directly.
        init_m = context.get("initial_model_key")
        if init_m and "initial_model_key" not in args and "_initial_model_key" not in args:
            args["_initial_model_key"] = init_m

        # Propagate tool timeout for subprocess-based tools
        tool_timeout = context.get("tool_timeout_sec")
        repl_tool_timeout = args.pop("_egg_tool_timeout_sec", None)
        if repl_tool_timeout is not None:
            tool_timeout = repl_tool_timeout
        if tool_timeout is not None and "_tool_timeout_sec" not in args:
            args["_tool_timeout_sec"] = tool_timeout

        # Propagate cancel check callback for interruptible tools
        cancel_check = context.get("cancel_check")
        if cancel_check is not None and "_cancel_check" not in args:
            args["_cancel_check"] = cancel_check

        return impl(args)


def _register_builtin_tools(reg: ToolRegistry) -> None:
    """Register Egg's current built-in tools into ``reg``.

    Later phases split these registrations into feature-bundle plugins. Keeping
    the current implementation behind this function lets the plugin manager own
    registry construction without changing tool behavior yet.
    """

    _populate_default_tools(reg)


def create_tool_registry() -> ToolRegistry:
    """Create a plugin-populated ToolRegistry with Egg's built-in tools."""

    from .builtin_plugins import ExecutionPlugin, SessionPlugin, SkillsPlugin, SubagentsPlugin
    from .plugins import FunctionPlugin, ToolPluginContext, register_plugins

    reg = ToolRegistry()
    register_plugins(
        ToolPluginContext(tool_registry=reg),
        [
            SkillsPlugin(),
            ExecutionPlugin(),
            SessionPlugin(),
            SubagentsPlugin(),
            FunctionPlugin("legacy_builtin_tools", "0", lambda context: _register_builtin_tools(context.tool_registry)),
        ],
    )
    return reg


# Default tools similar to chat.sh
def create_default_tools() -> ToolRegistry:
    """Create a ToolRegistry with the default set of tools.

    Returns a registry pre-populated with common tools:
    - bash: Execute shell commands
    - python: Execute Python scripts
    - spawn_agent: Create child threads for delegation
    - spawn_agent_auto: Create auto-approved child threads
    - web_search: Web search via the configured backend (SearXNG by default)
    - fetch_url: Fetch and extract readable markdown for a URL
    - wait: Synchronize on child thread completion

    Returns:
        ToolRegistry with default tools registered.
    """
    return create_tool_registry()


def _populate_default_tools(reg: ToolRegistry) -> None:
    import os, json as _json

    # web_search / fetch_url backed by a pluggable WebBackend.
    # Default backend is SearXNG; override with EGG_WEB_BACKEND=tavily
    # to use Tavily's API instead.
    from .web import WebBackendError, get_backend as _get_web_backend

    # Default result count: 10 (roughly SearXNG's natural "one page"
    # once engines are deduplicated). Override per-process via
    # EGG_WEB_MAX_RESULTS, or per-call via the tool's max_results arg.
    _WEB_RESULTS_CAP = 25

    def _resolve_max_results(args: Dict[str, Any]) -> int:
        raw = args.get('max_results')
        if raw is None:
            raw = os.environ.get('EGG_WEB_MAX_RESULTS')
        try:
            n = int(raw) if raw is not None and str(raw).strip() != '' else 10
        except (TypeError, ValueError):
            n = 10
        if n < 1:
            n = 1
        if n > _WEB_RESULTS_CAP:
            n = _WEB_RESULTS_CAP
        return n

    def _web_search(args: Dict[str, Any]):
        query = str(args.get('query') or '').strip()
        if not query:
            return 'Error: "query" is required.'
        n = _resolve_max_results(args)
        try:
            backend = _get_web_backend()
            results = backend.search(query, max_results=n)
        except WebBackendError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error: web_search failed: {e}"
        if not results:
            return "No results."
        lines = []
        for r in results:
            if not (r.title or r.url):
                continue
            snippet = (r.snippet or '').strip().replace('\n', ' ')
            if len(snippet) > 200:
                snippet = snippet[:200].rstrip() + '…'
            if snippet:
                lines.append(f"- {r.title}  {r.url}\n    {snippet}")
            else:
                lines.append(f"- {r.title}  {r.url}")
        return "\n".join(lines)

    def _fetch_url(args: Dict[str, Any]):
        url = str(args.get('url') or '').strip()
        if not url:
            return 'Error: "url" is required.'
        try:
            backend = _get_web_backend()
            return backend.fetch(url)
        except WebBackendError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error: fetch_url failed: {e}"

    _search_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query."},
            "max_results": {
                "type": "integer",
                "description": (
                    "Maximum number of results to return "
                    f"(default 10, max {_WEB_RESULTS_CAP})."
                ),
                "minimum": 1,
                "maximum": _WEB_RESULTS_CAP,
            },
        },
        "required": ["query"],
    }
    _fetch_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to fetch."},
        },
        "required": ["url"],
    }

    reg.register(
        name='web_search',
        description=(
            'Perform a web search and return results with titles, URLs, and short snippets. '
            f'Defaults to 10 results (cap {_WEB_RESULTS_CAP}); pass max_results to adjust. '
            'Backend is selected via EGG_WEB_BACKEND (default: searxng).'
        ),
        parameters_schema=_search_schema,
        impl=_web_search,
    )
    reg.register(
        name='fetch_url',
        description=(
            'Fetch and extract readable markdown from a URL. Use this when you '
            'already know the page URL.'
        ),
        parameters_schema=_fetch_schema,
        impl=_fetch_url,
    )

    # Note: agent-oriented tools like popContext/spawn_agent are excluded from default registry
    # to prevent unintended tool calls in basic chats. The UI layer can register them explicitly.

    return None

