from __future__ import annotations

import json
from typing import Any, Callable, Dict, List


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

        # Similarly, propagate the calling thread's model key so tools can
        # inherit it when the caller did not specify an explicit
        # initial_model_key. We keep this under a reserved key so that a
        # user-specified initial_model_key always wins.
        init_m = context.get("initial_model_key")
        if init_m and "initial_model_key" not in args and "_initial_model_key" not in args:
            args["_initial_model_key"] = init_m

        return impl(args)


# Default tools similar to chat.sh
def create_default_tools() -> ToolRegistry:
    import asyncio, subprocess, sys, os, json as _json, time as _time
    from io import StringIO
    from pathlib import Path

    reg = ToolRegistry()

    # bash
    def _bash(args: Dict[str, Any]):
        from .sandbox import get_thread_sandbox_config, wrap_argv_for_sandbox_with_settings
        from .db import ThreadsDB

        script = args.get('script', '')
        # Mirror the async runner: build an explicit argv and optionally
        # wrap it in the sandbox instead of relying on shell=True.
        base_argv = ['/bin/bash', '-lc', script]

        # Honour per-thread sandbox settings when available.
        tid = (args.get('_thread_id') or '').strip()
        if tid:
            try:
                db = ThreadsDB()
                sb = get_thread_sandbox_config(db, tid)
                argv = wrap_argv_for_sandbox_with_settings(
                    base_argv,
                    enabled=sb.enabled,
                    settings=sb.settings,
                )
            except Exception:
                argv = base_argv
        else:
            # No thread context: default behaviour (use default policy).
            from .sandbox import wrap_argv_for_sandbox
            argv = wrap_argv_for_sandbox(base_argv)
        res = subprocess.run(argv, capture_output=True, text=True)
        out = ''
        if res.stdout:
            out += f"--- STDOUT ---\n{res.stdout.strip()}\n"
        if res.stderr:
            out += f"--- STDERR ---\n{res.stderr.strip()}\n"
        return out.strip() or "--- The command executed successfully and produced no output ---"

    reg.register(
        name='bash',
        description='Execute a bash script and return combined stdout/stderr.',
        parameters_schema={
            "type": "object",
            "properties": {"script": {"type": "string"}},
            "required": ["script"],
        },
        impl=_bash,
    )

    # python
    def _python(args: Dict[str, Any]):
        """Execute Python in a subprocess (sandboxed when enabled).

        We intentionally avoid ``exec()`` in-process because sandboxing
        must be enforceable and because in-process execution can mutate
        global interpreter state.
        """

        from .sandbox import get_thread_sandbox_config, wrap_argv_for_sandbox_with_settings
        from .db import ThreadsDB

        script = args.get('script', '')
        thread_id = (args.get('_thread_id') or '').strip()

        # Build argv for python -c.
        base_argv = [sys.executable or 'python3', '-c', script]

        # Apply sandbox wrapper, respecting per-thread sandbox config.
        if thread_id:
            try:
                db = ThreadsDB()
                sb = get_thread_sandbox_config(db, thread_id)
                argv = wrap_argv_for_sandbox_with_settings(
                    base_argv,
                    enabled=sb.enabled,
                    settings=sb.settings,
                )
            except Exception:
                argv = base_argv
        else:
            from .sandbox import wrap_argv_for_sandbox
            argv = wrap_argv_for_sandbox(base_argv)

        res = subprocess.run(argv, capture_output=True, text=True)
        out = ''
        if res.stdout:
            out += f"--- STDOUT ---\n{res.stdout.strip()}\n"
        if res.stderr:
            out += f"--- STDERR ---\n{res.stderr.strip()}\n"
        return out.strip() or "--- The script executed successfully and produced no output ---"

    reg.register(
        name='python',
        description='Execute a Python script and return combined stdout/stderr.',
        parameters_schema={
            "type": "object",
            "properties": {"script": {"type": "string"}},
            "required": ["script"],
        },
        impl=_python,
    )

    # javascript (placeholder for remote-debugging execution; here we only echo input)
    def _javascript(args: Dict[str, Any]):
        script = args.get('script', '')
        url = args.get('url', '')
        return _json.dumps({"script": script[:200], "url": url})

    reg.register(
        name='javascript',
        description='Execute JavaScript in a browser via remote debugging (app layer should implement).',
        parameters_schema={
            "type": "object",
            "properties": {"script": {"type": "string"}, "url": {"type": "string"}},
            "required": ["script"],
        },
        impl=_javascript,
    )

    # spawn_agent: create a child thread under a given parent, mirroring
    # the behaviour of the /spawn UI command but in a UI-agnostic way.
    def _spawn_agent(args: Dict[str, Any]):
        from .db import ThreadsDB
        from .api import create_child_thread, append_message, create_snapshot

        # Parent is either explicitly provided (for UI/user commands) or
        # inferred from the calling thread context (_thread_id injected by
        # ToolRegistry.execute when called from a runner).
        parent_id = (args.get('parent_thread_id') or args.get('_thread_id') or '').strip()
        if not parent_id:
            return 'Error: parent_thread_id is required.'

        # Optional fields
        label = (args.get('label') or 'spawn').strip() or 'spawn'
        user_text = (args.get('context_text') or '').strip() or 'Spawned task'
        # initial_model_key may be explicitly provided by the UI, or
        # inherited from the calling thread via _initial_model_key
        # injected by the runner context.
        initial_model_key = (args.get('initial_model_key')
                             or args.get('_initial_model_key')
                             or '').strip() or None
        system_prompt = (args.get('system_prompt') or '').strip() or None

        db = ThreadsDB()

        # If no explicit system prompt was provided, try to inherit it from
        # the parent thread's first system message. This mirrors the
        # interactive app's behaviour where child threads reuse the global
        # system prompt. As a final fallback, use a generic helper prompt.
        if not system_prompt:
            try:
                row = db.get_thread(parent_id)
                # Build a fresh snapshot if needed so we see system
                # messages even for recently created parents.
                if row and not row.snapshot_json:
                    try:
                        create_snapshot(db, parent_id)
                        row = db.get_thread(parent_id)
                    except Exception:
                        pass
                if row and row.snapshot_json:
                    try:
                        snap = _json.loads(row.snapshot_json)
                        msgs = snap.get('messages', []) or []
                        for m in msgs:
                            try:
                                if m.get('role') == 'system' and isinstance(m.get('content'), str):
                                    system_prompt = m.get('content') or None
                                    if system_prompt:
                                        break
                            except Exception:
                                continue
                    except Exception:
                        pass
            except Exception:
                pass
        if not system_prompt:
            system_prompt = 'You are a helpful assistant.'

        # Create child thread
        try:
            child = create_child_thread(db, parent_id, name=label, initial_model_key=initial_model_key)
        except Exception as e:
            return f"Error: failed to create child thread: {e}"

        # Sandbox configuration inheritance is handled by
        # eggthreads.api.create_child_thread.

        # Seed the child with the resolved system prompt.
        try:
            append_message(db, child, 'system', system_prompt)
        except Exception:
            # Best-effort; child thread still exists.
            pass

        # Attach the requested user/context text
        try:
            append_message(db, child, 'user', user_text)
        except Exception as e:
            return f"Error: created child {child} but failed to append user message: {e}"

        # Mirror /spawn: if an initial_model_key was provided, record a
        # model.switch event so downstream tools and UIs can resolve the
        # effective model via current_thread_model().
        if initial_model_key:
            try:
                from .api import set_thread_model
                set_thread_model(db, child, initial_model_key, reason='spawn_agent initial model')
            except Exception:
                pass

        try:
            create_snapshot(db, child)
        except Exception:
            # Non-fatal
            pass

        return child

    reg.register(
        name='spawn_agent',
        description=(
            'Spawn a child agent as a new thread under the current task. '
            'Use this whenever you want to delegate a sub-problem to a '
            'child agent. Provide a short natural-language description of '
            'the sub-task in context_text. The child agent will run '
            'independently and eventually produce an assistant message as '
            'its result. To retrieve that result, call the "wait" tool on '
            'the returned thread id and read the last assistant message.'
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                # The parent thread is always the current calling thread;
                # the model never needs to provide its id explicitly.
                "context_text": {"type": "string"},
                "label": {"type": "string"},
                "initial_model_key": {"type": "string"},
                "system_prompt": {"type": "string"},
            },
            # Models must provide a short description of the sub-task.
            "required": ["context_text"],
        },
        impl=_spawn_agent,
        # Exposed to the model so it can explicitly spawn child agents.
        local_only=False,
    )

    # spawn_agent_auto: like spawn_agent, but also enables global tool
    # call auto-approval for the spawned child thread so it can execute
    # tools without further per-call approvals.
    def _spawn_agent_auto(args: Dict[str, Any]):
        from .db import ThreadsDB
        from .api import create_child_thread, append_message, create_snapshot

        parent_id = (args.get('parent_thread_id') or args.get('_thread_id') or '').strip()
        if not parent_id:
            return 'Error: parent_thread_id is required.'

        label = (args.get('label') or 'spawn_auto').strip() or 'spawn_auto'
        user_text = (args.get('context_text') or '').strip() or 'Spawned task'
        initial_model_key = (args.get('initial_model_key')
                             or args.get('_initial_model_key')
                             or '').strip() or None
        system_prompt = (args.get('system_prompt') or '').strip() or None

        db = ThreadsDB()

        # Inherit or default system prompt as in _spawn_agent.
        if not system_prompt:
            try:
                row = db.get_thread(parent_id)
                if row and not row.snapshot_json:
                    try:
                        create_snapshot(db, parent_id)
                        row = db.get_thread(parent_id)
                    except Exception:
                        pass
                if row and row.snapshot_json:
                    try:
                        snap = _json.loads(row.snapshot_json)
                        msgs = snap.get('messages', []) or []
                        for m in msgs:
                            try:
                                if m.get('role') == 'system' and isinstance(m.get('content'), str):
                                    system_prompt = m.get('content') or None
                                    if system_prompt:
                                        break
                            except Exception:
                                continue
                    except Exception:
                        pass
            except Exception:
                pass
        if not system_prompt:
            system_prompt = 'You are a helpful assistant.'

        # Create child thread
        try:
            child = create_child_thread(db, parent_id, name=label, initial_model_key=initial_model_key)
        except Exception as e:
            return f"Error: failed to create child thread: {e}"

        # Sandbox configuration inheritance is handled by
        # eggthreads.api.create_child_thread.

        # Seed system prompt and user text
        try:
            append_message(db, child, 'system', system_prompt)
        except Exception:
            pass
        try:
            append_message(db, child, 'user', user_text)
        except Exception as e:
            return f"Error: created child {child} but failed to append user message: {e}"

        # Model marker: if we know the initial model, record a
        # model.switch event so tools and UIs can resolve the effective
        # model via current_thread_model().
        if initial_model_key:
            try:
                from .api import set_thread_model
                set_thread_model(db, child, initial_model_key, reason='spawn_agent_auto initial model')
            except Exception:
                pass

        # Record a global auto-approval decision for this child thread so
        # it can execute tools without further per-call approvals.
        try:
            db.append_event(
                event_id=os.urandom(10).hex(),
                thread_id=child,
                type_='tool_call.approval',
                msg_id=None,
                invoke_id=None,
                payload={
                    'decision': 'global_approval',
                    'reason': 'spawn_agent_auto enabled global tool auto-approval for this thread',
                },
            )
        except Exception:
            pass

        try:
            create_snapshot(db, child)
        except Exception:
            pass

        return child

    reg.register(
        name='spawn_agent_auto',
        description=(
            'Like spawn_agent, but configures the spawned child thread to '
            'have global tool auto-approval. The child agent can call '
            'tools without further approval events. Use context_text to '
            'describe the delegated sub-task, then use the "wait" tool on '
            'the returned thread id to read its final assistant message.'
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "context_text": {"type": "string"},
                "label": {"type": "string"},
                "initial_model_key": {"type": "string"},
                "system_prompt": {"type": "string"},
            },
            "required": ["context_text"],
        },
        impl=_spawn_agent_auto,
        local_only=False,
    )

    # replace_between
    def _replace_between(args: Dict[str, Any]):
        file_path = args.get('file_path', '')
        start_text = args.get('start_text', '')
        end_text = args.get('end_text', '')
        new_text = args.get('new_text', '')
        p = Path(file_path)
        content = p.read_text() if p.exists() else ''
        sidx = content.find(start_text)
        if sidx == -1:
            return "Error: start_text not found."
        eidx = content.find(end_text, sidx + len(start_text))
        if eidx == -1:
            return "Error: end_text not found after start_text."
        new_content = content[:sidx] + new_text + content[eidx + len(end_text):]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(new_content)
        return "Success: replaced region."

    reg.register(
        name='replace_between',
        description='Replace the first region between two exact string boundaries (start_text and the first subsequent end_text) with new_text. Exact literal matching only (no regex). The boundaries themselves are also replaced. Works across line breaks.',
        parameters_schema={
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "start_text": {"type": "string"},
                "end_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            "required": ["file_path", "start_text", "end_text", "new_text"],
        },
        impl=_replace_between,
    )

    # search_tavily (simple wrapper; requires TAVILY_API_KEY env var)
    def _search_tavily(args: Dict[str, Any]):
        import os as _os, requests as _requests
        query = args.get('query', '')
        api_key = _os.environ.get('TAVILY_API_KEY')
        if not api_key:
            return 'Error: TAVILY_API_KEY not set in environment.'
        try:
            resp = _requests.post(
                'https://api.tavily.com/search',
                json={'query': query, 'max_results': 5, 'include_answer': False, 'search_depth': 'basic'},
                headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {api_key}'},
                timeout=20,
            )
            if resp.status_code != 200:
                return f"Error: Tavily API status {resp.status_code}: {resp.text[:400]}"
            data = resp.json()
            results = data.get('results') or data.get('data') or []
            lines = []
            for r in results[:5]:
                title = (r.get('title') or '').strip()
                url = (r.get('url') or r.get('link') or '').strip()
                if title or url:
                    lines.append(f"- {title}  {url}")
            return "\n".join(lines) or "No results."
        except Exception as e:
            return f"Error: Tavily request failed: {e}"

    reg.register(
        name='search_tavily',
        description='Perform a web search (using Tavily) and return up to 5 results with titles and URLs.',
        parameters_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        impl=_search_tavily,
    )

    # wait: synchronize on other threads and return their last assistant
    # message when finished. This is a standard tool so that both the
    # assistant and user commands (/wait) can use the same implementation.
    def _wait_tool(args: Dict[str, Any]):
        from .tool_state import thread_state
        from .db import ThreadsDB

        tids_arg = args.get('thread_ids') or args.get('threads') or args.get('thread_id')
        if isinstance(tids_arg, str):
            thread_ids = [tids_arg]
        elif isinstance(tids_arg, list):
            thread_ids = [str(t) for t in tids_arg if isinstance(t, (str, int))]
        else:
            return 'Error: "thread_ids" must be a string or a list of strings.'
        if not thread_ids:
            return 'Error: no valid thread_ids provided.'

        timeout = args.get('timeout_sec') or args.get('timeout')
        try:
            timeout_sec = float(timeout) if timeout is not None else None
        except Exception:
            timeout_sec = None

        db = ThreadsDB()

        # Helper: get last assistant content for a thread from its snapshot
        def _last_assistant_content(tid: str) -> str:
            row = db.get_thread(tid)
            if not row or not row.snapshot_json:
                return ''
            try:
                snap = _json.loads(row.snapshot_json)
            except Exception:
                return ''
            msgs = snap.get('messages', []) or []
            for m in reversed(msgs):
                try:
                    if m.get('role') == 'assistant' and isinstance(m.get('content'), str):
                        return m.get('content') or ''
                except Exception:
                    continue
            return ''

        start = _time.time()
        finished: Dict[str, bool] = {tid: False for tid in thread_ids}
        results: Dict[str, str] = {}

        # Poll until all threads reach waiting_user or timeout
        while True:
            all_done = True
            for tid in thread_ids:
                if finished.get(tid):
                    continue
                try:
                    st = thread_state(db, tid)
                except Exception:
                    st = 'unknown'
                if st == 'waiting_user':
                    results[tid] = _last_assistant_content(tid)
                    finished[tid] = True
                else:
                    all_done = False
            if all_done:
                break
            if timeout_sec is not None and (_time.time() - start) >= timeout_sec:
                break
            _time.sleep(0.2)

        # Format summary
        lines: list[str] = []
        for tid in thread_ids:
            short = tid[-8:]
            if finished.get(tid):
                content = results.get(tid) or '(no assistant content found)'
                lines.append(f"Thread {short} finished. Last assistant message:\n{content}")
            else:
                try:
                    st = thread_state(db, tid)
                except Exception:
                    st = 'unknown'
                lines.append(f"Thread {short} not finished (state={st}).")
        return "\n\n".join(lines)

    reg.register(
        name='wait',
        description=(
            'Wait for one or more threads to finish and return their last '
            'assistant message. A thread is considered finished when its '
            "state is 'waiting_user'. Optional timeout_sec limits how long to wait."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "thread_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of thread_ids to wait for.",
                },
                "timeout_sec": {
                    "type": "number",
                    "description": "Maximum seconds to wait before returning.",
                },
            },
            "required": ["thread_ids"],
        },
        impl=_wait_tool,
        # Exposed to the model so it can explicitly wait for spawned
        # child agents and read their final responses.
        local_only=False,
    )

    # Note: agent-oriented tools like popContext/spawn_agent are excluded from default registry
    # to prevent unintended tool calls in basic chats. The UI layer can register them explicitly.

    return reg

