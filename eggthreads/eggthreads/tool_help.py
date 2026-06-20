from __future__ import annotations

"""Shared tool help/introspection rendering for /toolInfo and tool_help.

The public surfaces deliberately share this module:

* ``/toolInfo <name>`` uses it for human-facing command output.
* the LLM-facing ``tool_help`` tool uses it for on-demand, context-aware tool
  descriptions without bloating every provider tool schema.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


HELP_TOOL_NAME = "tool_help"


@dataclass(frozen=True)
class ToolHelpRenderResult:
    """Rendered help plus minimal status for command handlers."""

    text: str
    found: bool = True
    tool_name: str | None = None


# Detailed help for every default LLM-facing tool.  The runtime renderer also
# includes the canonical JSON schema description and parameter descriptions, so
# these entries should focus on behavior, good usage, and important caveats.
BUILTIN_TOOL_HELP_DETAILS: dict[str, dict[str, Any]] = {
    "answer_user_while_preserving_llm_turn": {
        "details": "Send a visible interim assistant note to the user without ending the current assistant/tool workflow.",
        "use_when": [
            "You owe the user a status update while continuing a longer workflow.",
            "You need to answer a side question but still plan to keep working in the same turn.",
        ],
        "notes": [
            "This is not a substitute for the final assistant response.",
            "Keep messages concise and user-facing; do not include hidden chain-of-thought.",
        ],
        "examples": ['{"message": "I found the failing path and am adding a focused test now."}'],
    },
    "get_user_message_while_preserving_llm_turn": {
        "details": "Show a visible assistant note, wait for the next normal user message, then return that user message as the tool result.",
        "use_when": [
            "You must ask a blocking clarification before safely continuing the current workflow.",
            "You are intentionally keeping an infinite/manager-worker style turn open.",
        ],
        "notes": [
            "Use sparingly: it blocks until the user replies or the tool is interrupted.",
            "The user reply is consumed as tool input rather than a normal provider-visible turn.",
        ],
        "examples": ['{"assistant_note": "Which backend should I validate first?"}'],
    },
    "skill": {
        "details": "List, search, or load Egg skill documents containing workflow instructions and snippets.",
        "use_when": [
            "A named skill is relevant to the requested workflow.",
            "You want to search packaged skills by topic before starting.",
        ],
        "notes": [
            "Skills are read-only documents; loading a skill does not install new runtime APIs.",
            "After loading a skill, adapt its instructions to the current task rather than copying blindly.",
        ],
        "examples": ['{}', '{"query": "worker"}', '{"name": "rlm"}'],
    },
    "compact_thread": {
        "details": "Move the start of future provider/API context for the thread while preserving the visible event history.",
        "use_when": [
            "The user asks for compaction or a specific context reset point.",
            "Context pressure makes a faithful summarized new start appropriate.",
        ],
        "notes": [
            "Do not compact in the middle of substantive work merely because the tool exists.",
            "When writing a summary checkpoint, write the summary first, then compact if requested by the workflow.",
        ],
        "examples": ['{"start_message": "last_user"}'],
    },
    "read_long_tool_output": {
        "details": "Read a bounded chunk from a long tool-output artifact using its short artifact id.",
        "use_when": [
            "A prior tool result was truncated into a long-output artifact and you need a specific chunk.",
            "You need to inspect descendant-thread long output without flooding the prompt.",
        ],
        "notes": [
            "Use the short artifact id shown in the preview, not arbitrary paths.",
            "Read only the chunks needed for the task.",
        ],
        "examples": ['{"artifact_id": "abc123", "chunk_number": 1}'],
    },
    "bash": {
        "details": "Execute a non-interactive bash script in the project working directory and return combined stdout/stderr.",
        "use_when": [
            "You need shell tools for repository inspection, tests, git status, or small automation.",
            "A command-line tool is more direct than Python for the task.",
        ],
        "notes": [
            "Prefer bounded, focused commands and set timeout for potentially long runs.",
            "Do not use destructive commands unless they are explicitly required and safe.",
        ],
        "examples": ['{"script": "git status --short && pytest -q eggthreads/tests/test_tool_help.py", "timeout": 120}'],
    },
    "python": {
        "details": "Execute a standalone Python script and return combined stdout/stderr.",
        "use_when": [
            "You need structured parsing, repository analysis, or a short deterministic script.",
            "A one-shot Python process is preferable to maintaining REPL state.",
        ],
        "notes": [
            "This is not the persistent REPL; state is not preserved between calls.",
            "Use focused scripts and bounded output.",
        ],
        "examples": ['{"script": "from pathlib import Path; print(Path.cwd())"}'],
    },
    "generate_image": {
        "details": "Generate provider-backed image artifacts through Egg's configured image-generation service.",
        "use_when": [
            "The user asks you to create image output, not merely describe an image.",
            "A prompt should be sent to a configured image-generation backend and stored as provider-output artifacts.",
        ],
        "notes": [
            "Omit model/backend to use the configured default image-generation model.",
            "Generated bytes are stored as provider-output artifacts; the tool result contains metadata and artifact references, not inline bytes.",
            "Backends using openai_responses_image_tool generate one image per call; omit n or use n=1 for them.",
            "ChatGPT/Codex OAuth subscription providers currently do not expose the Responses image_generation tool to Egg; use OpenAI API image backends for generation.",
            "Use /saveProviderArtifact or /attachOutput style flows when the user needs export or reuse.",
        ],
        "examples": [
            '{"prompt": "A watercolor egg-shaped spaceship in a pine forest"}',
            '{"model": "OpenAI Image: gpt-image-1", "prompt": "A tiny robot painting an egg", "size": "1024x1024", "output_format": "png"}',
        ],
    },
    "python_repl": {
        "details": "Run code in this thread's persistent Python REPL session, with thread context helpers preloaded.",
        "use_when": [
            "You need to keep large outputs/state in memory across tool calls.",
            "You need exact transcript inspection through hydrated helpers such as search_thread or get_message.",
        ],
        "notes": [
            "Prefer this over repeated one-shot Python when persistent variables simplify the work.",
            "Show bounded previews or final findings rather than dumping large REPL data.",
        ],
        "examples": ['{"code": "print(len(all_messages))"}'],
    },
    "bash_repl": {
        "details": "Run shell code in this thread's persistent bash REPL session.",
        "use_when": [
            "You need shell session state, environment, or repeated commands sharing setup.",
            "A persistent shell is simpler than one-shot bash calls.",
        ],
        "notes": [
            "Use bounded commands and avoid leaving noisy background processes.",
            "For simple isolated commands, the regular bash tool is usually enough.",
        ],
        "examples": ['{"script": "pwd && git status --short"}'],
    },
    "spawn_agent": {
        "details": "Spawn a child agent/thread for delegated sub-work under the current task.",
        "use_when": [
            "A sub-problem can be solved independently and later synthesized.",
            "You want a child to inspect, analyze, or implement a focused slice.",
        ],
        "notes": [
            "Give concrete scope and retrieve the result with wait.",
            "For normal manager/worker coding, prefer one long-lived worker rather than rotating workers.",
        ],
        "examples": ['{"label": "review", "context_text": "Inspect the failing test and report the root cause.", "share_session": false, "share_repl": false}'],
    },
    "spawn_agent_auto": {
        "details": "Spawn a child agent with global tool auto-approval enabled.",
        "use_when": [
            "You intentionally want a delegated worker to use tools without per-call approval friction.",
            "A coding/review worker needs to run tests and inspect files autonomously.",
        ],
        "notes": [
            "Only use auto-approval when it is appropriate for the task and repository safety.",
            "Still provide narrow scope and review the result before reporting to the user.",
        ],
        "examples": ['{"label": "impl", "context_text": "Implement one focused test-only slice and report.", "share_session": false, "share_repl": false}'],
    },
    "send_message_to_child": {
        "details": "Append guidance to an existing child/descendant thread so it can continue from its context.",
        "use_when": [
            "A worker has reported a status and you want it to continue with the next slice.",
            "You need to answer a child thread waiting in get_user_message_while_preserving_llm_turn.",
        ],
        "notes": [
            "Prefer continuing a reliable primary worker over spawning a replacement.",
            "By default it refuses to message a running child; wait or inspect status first.",
        ],
        "examples": ['{"child_thread_id": "thread-id", "message": "Continue with the next focused test slice."}'],
    },
    "continue_subthread": {
        "details": "Repair or continue a child/descendant thread after LLM, runner, or session failures.",
        "use_when": [
            "A child thread ended due to infrastructure/transient failure before summarizing.",
            "You need the child to resume from its last stable state.",
        ],
        "notes": [
            "Inspect child status first so real implementation failures are not papered over.",
            "Use this for repair, not as a substitute for fixing failing tests or design blockers.",
        ],
        "examples": ['{"child_thread_id": "thread-id"}'],
    },
    "get_child_status": {
        "details": "Inspect child or descendant thread state, context pressure, active notes, and recent errors without waiting.",
        "use_when": [
            "You need to decide whether to keep waiting, repair, or intervene in a child thread.",
            "A wait timed out and you need a bounded status check.",
        ],
        "notes": [
            "Omit child_thread_ids to inspect direct children.",
            "Use max_errors to keep error output bounded.",
        ],
        "examples": ['{"child_thread_ids": ["thread-id"], "max_errors": 5}'],
    },
    "wait": {
        "details": "Wait for one or more child threads to finish and return their last assistant message.",
        "use_when": [
            "You delegated work and need the child result before synthesizing or continuing.",
            "You are running a bounded manager/worker wait loop.",
        ],
        "notes": [
            "Use a timeout for long-running work so you can inspect progress and retain control.",
            "A thread is considered finished when it reaches waiting_user state.",
        ],
        "examples": ['{"thread_ids": ["thread-id"], "timeout": 300}'],
    },
    "web_search": {
        "details": "Search the web through Egg's configured provider/fallback search backend and return titles, URLs, and snippets.",
        "use_when": [
            "Current external information may be needed and no specific URL is known.",
            "You need candidate sources before fetching exact pages.",
        ],
        "notes": [
            "Use precise queries and then fetch authoritative URLs when details matter.",
            "Respect max_results to keep output bounded.",
        ],
        "examples": ['{"query": "OpenAI image generation API gpt-image-1 size output_format", "max_results": 5}'],
    },
    "fetch_url": {
        "details": "Fetch and extract readable markdown from a known URL.",
        "use_when": [
            "You already know the page URL and need its content.",
            "A web_search result needs verification from the source page.",
        ],
        "notes": [
            "Fetching can fail or extract imperfectly depending on site behavior.",
            "Prefer authoritative sources for API behavior, pricing, or documentation.",
        ],
        "examples": ['{"url": "https://platform.openai.com/docs"}'],
    },
    HELP_TOOL_NAME: {
        "details": "Describe Egg tools on demand, including parameters, examples, current availability, and dynamic context for tools that need it.",
        "use_when": [
            "You need to know how to call a tool or which options/model keys are currently configured.",
            "You want a concise list of tools available in the current thread.",
        ],
        "notes": [
            "This is read-only and should not execute the target tool.",
            "For generate_image, the help includes configured image-generation backends when available.",
        ],
        "examples": ['{}', '{"tool_name": "generate_image"}', '{"tool_name": "bash", "include_schema": true}'],
    },
}


def missing_detailed_tool_help_entries(tool_names: list[str] | tuple[str, ...] | set[str]) -> list[str]:
    """Return registered tool names that lack a curated detailed-help entry."""

    return sorted(name for name in tool_names if name not in BUILTIN_TOOL_HELP_DETAILS)


def collect_tool_entries(registry: Any | None = None) -> dict[str, dict[str, Any]]:
    """Return normalized introspection entries from a ToolRegistry.

    The shape intentionally matches the older ``available_tools()`` command
    helper while adding optional capabilities metadata.
    """

    if registry is None:
        from .tools import create_default_tools

        registry = create_default_tools()

    out: dict[str, dict[str, Any]] = {}
    for name, entry in getattr(registry, "_tools", {}).items():
        capabilities = entry.get("capabilities")
        if hasattr(capabilities, "to_dict"):
            capabilities_value = capabilities.to_dict()
        elif isinstance(capabilities, Mapping):
            capabilities_value = dict(capabilities)
        else:
            capabilities_value = {}
        out[str(name)] = {
            "spec": entry.get("spec") or {},
            "local_only": bool(entry.get("local_only", False)),
            "capabilities": capabilities_value,
        }
    return out


def _bool_arg(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        token = value.strip().lower()
        if token in {"1", "true", "yes", "on"}:
            return True
        if token in {"0", "false", "no", "off"}:
            return False
    return default


def _function_spec(spec: Mapping[str, Any]) -> Mapping[str, Any]:
    fn = spec.get("function") if isinstance(spec, Mapping) else None
    return fn if isinstance(fn, Mapping) else spec


def _tool_description(entry: Mapping[str, Any]) -> str:
    spec = entry.get("spec") if isinstance(entry, Mapping) else None
    fn = _function_spec(spec if isinstance(spec, Mapping) else {})
    return str(fn.get("description") or "").strip()


def _tool_parameters(entry: Mapping[str, Any]) -> Mapping[str, Any]:
    spec = entry.get("spec") if isinstance(entry, Mapping) else None
    fn = _function_spec(spec if isinstance(spec, Mapping) else {})
    params = fn.get("parameters")
    return params if isinstance(params, Mapping) else {}


def _resolve_tool_name(entries: Mapping[str, Any], requested: str) -> str | None:
    if requested in entries:
        return requested
    requested_lower = requested.strip().lower()
    for name in entries:
        if name.lower() == requested_lower:
            return name
    return None


def _thread_tools_config(db: Any, thread_id: str | None) -> Any | None:
    if db is None or not thread_id:
        return None
    try:
        from .tools_config import get_thread_tools_config

        return get_thread_tools_config(db, thread_id)
    except Exception:
        return None


def _availability(name: str, entry: Mapping[str, Any], tools_cfg: Any | None) -> tuple[bool, str]:
    if bool(entry.get("local_only", False)):
        return False, "local-only (not exposed to the LLM)"
    if tools_cfg is not None:
        if not bool(getattr(tools_cfg, "llm_tools_enabled", True)):
            return False, "LLM tools disabled for this thread"
        try:
            if not tools_cfg.is_tool_allowed(name):
                return False, "not allowed for this thread"
        except Exception:
            pass
    return True, "available to the LLM in this context"


def _format_parameter_lines(params: Mapping[str, Any]) -> list[str]:
    properties = params.get("properties") if isinstance(params, Mapping) else None
    required_values = params.get("required") if isinstance(params, Mapping) else None
    required = {str(item) for item in required_values} if isinstance(required_values, list) else set()
    if not isinstance(properties, Mapping) or not properties:
        return ["- No parameters."]

    lines: list[str] = []
    for name, raw in properties.items():
        prop = raw if isinstance(raw, Mapping) else {}
        typ = prop.get("type")
        if isinstance(typ, list):
            typ_text = " | ".join(str(item) for item in typ)
        elif typ:
            typ_text = str(typ)
        else:
            typ_text = "any"
        req = "required" if str(name) in required else "optional"
        desc = str(prop.get("description") or "").strip()
        enum = prop.get("enum")
        suffix = f" — {desc}" if desc else ""
        if isinstance(enum, list) and enum:
            suffix += f" Allowed values: {', '.join(str(item) for item in enum)}."
        lines.append(f"- `{name}` ({typ_text}, {req}){suffix}")
    return lines


def _resolve_configured_model_key(models_config: Mapping[str, Any], key_or_alias: str) -> str | None:
    requested = str(key_or_alias or "").strip()
    if not requested:
        return None
    if requested in models_config:
        return requested
    requested_lower = requested.lower()
    for key, cfg_raw in models_config.items():
        if key.lower() == requested_lower:
            return key
        cfg = cfg_raw if isinstance(cfg_raw, Mapping) else {}
        aliases = cfg.get("alias")
        if isinstance(aliases, str):
            aliases = [aliases]
        if isinstance(aliases, list) and any(isinstance(alias, str) and alias.lower() == requested_lower for alias in aliases):
            return key
    return None


def _configured_model_default(models_config: Mapping[str, Any], providers_config: Mapping[str, Any]) -> tuple[str | None, str | None]:
    meta = providers_config.get("_meta") if isinstance(providers_config, Mapping) else None
    configured_default = meta.get("default_model") if isinstance(meta, Mapping) else None
    if isinstance(configured_default, str) and configured_default.strip():
        configured_default = configured_default.strip()
        resolved = _resolve_configured_model_key(models_config, configured_default)
        return (resolved or configured_default), configured_default
    first = next(iter(models_config.keys()), None) if models_config else None
    return first, None


def _default_image_generation_models_path(models_path: Any, explicit_path: Any) -> Path:
    if explicit_path:
        return Path(explicit_path)
    try:
        from eggllm.config import default_image_generation_models_path

        return default_image_generation_models_path(models_path or "models.json")
    except Exception:
        return Path(models_path or "models.json").with_name("image-generation-models.json")


def _generate_image_dynamic_help(*, raw_context: Mapping[str, Any]) -> list[str]:
    models_path = raw_context.get("models_path") or "models.json"
    image_generation_models_path = _default_image_generation_models_path(
        models_path,
        raw_context.get("image_generation_models_path"),
    )
    try:
        from eggllm.config import load_image_generation_models_config

        models_config, providers_config = load_image_generation_models_config(
            image_generation_models_path,
            models_path=models_path,
        )
    except Exception as e:
        return [
            "Dynamic image-generation context:",
            f"- Could not load image-generation model config: {e}",
        ]

    lines = ["Dynamic image-generation context:"]
    lines.append(f"- Config file: `{image_generation_models_path}`")
    if not models_config:
        lines.extend(
            [
                "- No image-generation models are currently configured.",
                "- Add entries to `image-generation-models.json`; provider credentials/base URLs still come from `models.json`.",
                "- Normal generation calls should omit `model` only after a default/configured backend exists.",
            ]
        )
        return lines

    default_model, configured_default = _configured_model_default(models_config, providers_config)
    if default_model:
        if configured_default and configured_default != default_model:
            lines.append(
                f"- Default model when `model`/`backend` is omitted: `{default_model}` "
                f"(configured as `{configured_default}`)"
            )
        else:
            lines.append(f"- Default model when `model`/`backend` is omitted: `{default_model}`")
    lines.append("- Available image-generation models:")
    for key, cfg_raw in models_config.items():
        cfg = cfg_raw if isinstance(cfg_raw, Mapping) else {}
        marker = " (default)" if default_model and key == default_model else ""
        lines.append(f"  - `{key}`{marker}")
        aliases = cfg.get("alias")
        if isinstance(aliases, str):
            aliases = [aliases]
        if isinstance(aliases, list) and aliases:
            lines.append(f"    aliases: {', '.join(f'`{alias}`' for alias in aliases if isinstance(alias, str))}")
        provider = cfg.get("provider")
        model_name = cfg.get("model_name")
        api_type = cfg.get("api_type")
        provider_cfg = providers_config.get(provider) if isinstance(provider, str) else None
        if provider:
            lines.append(f"    provider: `{provider}`")
        if api_type:
            lines.append(f"    api_type: `{api_type}`")
            if str(api_type).strip().lower().replace("-", "_") == "openai_responses_image_tool":
                lines.append("    option note: one image per call; omit `n` or use `n=1`")
                provider_auth = str((provider_cfg or {}).get("auth_type") or "api_key").strip().lower() if isinstance(provider_cfg, Mapping) else "api_key"
                provider_base = str((provider_cfg or {}).get("api_base") or "").strip().lower() if isinstance(provider_cfg, Mapping) else ""
                if (provider == "openai-pro" and provider_auth == "chatgpt_oauth") or "chatgpt.com/backend-api/codex/responses" in provider_base:
                    lines.append("    availability note: ChatGPT/Codex OAuth does not currently expose image_generation here")
        if model_name:
            lines.append(f"    provider model: `{model_name}`")
        params = cfg.get("parameters") if isinstance(cfg.get("parameters"), Mapping) else {}
        configured_options = sorted(str(k) for k in params.keys()) if params else []
        if configured_options:
            lines.append(f"    configured option keys: {', '.join(f'`{key}`' for key in configured_options)}")
    lines.append("- Recommended call: omit `model` unless you need a non-default configured backend.")
    return lines


def _dynamic_help_lines(tool_name: str, *, raw_context: Mapping[str, Any]) -> list[str]:
    if tool_name == "generate_image":
        return _generate_image_dynamic_help(raw_context=raw_context)
    return []


def _detail_lines(tool_name: str) -> list[str]:
    detail = BUILTIN_TOOL_HELP_DETAILS.get(tool_name) or {}
    lines: list[str] = []
    details = str(detail.get("details") or "").strip()
    if details:
        lines.extend(["Detailed description:", details])
    for label, key in (("Use when", "use_when"), ("Notes", "notes"), ("Examples", "examples")):
        values = detail.get(key)
        if isinstance(values, list) and values:
            lines.append(f"{label}:")
            for value in values:
                lines.append(f"- {value}")
    return lines


def _render_tool_list(entries: Mapping[str, Mapping[str, Any]], *, tools_cfg: Any | None, include_unavailable: bool) -> ToolHelpRenderResult:
    lines = [
        "Egg tool help",
        "Use `tool_help` with `tool_name` to inspect one tool in detail.",
        "Example: `{\"tool_name\": \"generate_image\"}`",
        "",
        "Tools:",
    ]
    shown = 0
    for name in sorted(entries):
        entry = entries[name]
        available, status = _availability(name, entry, tools_cfg)
        if not include_unavailable and not available:
            continue
        description = _tool_description(entry)
        if not description:
            description = str((BUILTIN_TOOL_HELP_DETAILS.get(name) or {}).get("details") or "").strip()
        status_suffix = "" if available else f" [{status}]"
        lines.append(f"- `{name}`{status_suffix}: {description or 'No description available.'}")
        shown += 1
    if shown == 0:
        lines.append("- No tools are currently exposed in this context.")
    return ToolHelpRenderResult("\n".join(lines).strip(), found=True)


def render_tool_help(
    tool_name: str | None = None,
    *,
    registry: Any | None = None,
    entries: Mapping[str, Mapping[str, Any]] | None = None,
    db: Any = None,
    thread_id: str | None = None,
    raw_context: Mapping[str, Any] | None = None,
    include_schema: bool = False,
    include_unavailable: bool = False,
) -> ToolHelpRenderResult:
    """Render shared human/LLM tool help.

    ``registry`` should be the active ToolRegistry when available.  Passing
    ``entries`` is useful for tests and for /toolInfo's older available_tools()
    hook.  ``raw_context`` carries model/config paths for dynamic sections.
    """

    raw_context = raw_context if isinstance(raw_context, Mapping) else {}
    entries = dict(entries) if entries is not None else collect_tool_entries(registry)
    tools_cfg = _thread_tools_config(db, thread_id)

    requested = str(tool_name or "").strip()
    if not requested:
        return _render_tool_list(entries, tools_cfg=tools_cfg, include_unavailable=include_unavailable)

    resolved = _resolve_tool_name(entries, requested)
    if resolved is None:
        available_names = ", ".join(sorted(entries.keys())) or "(none)"
        return ToolHelpRenderResult(
            f"Tool '{requested}' not found.\nAvailable tools: {available_names}",
            found=False,
            tool_name=requested,
        )

    entry = entries[resolved]
    spec = entry.get("spec") if isinstance(entry, Mapping) else {}
    params = _tool_parameters(entry)
    available, status = _availability(resolved, entry, tools_cfg)
    capabilities = entry.get("capabilities") if isinstance(entry, Mapping) else None
    description = _tool_description(entry)

    lines = [
        f"Tool: {resolved}",
        f"Status: {status}",
        f"Local-only: {bool(entry.get('local_only', False))}",
    ]
    if description:
        lines.extend(["", "Summary:", description])

    detail_lines = _detail_lines(resolved)
    if detail_lines:
        lines.extend(["", *detail_lines])

    dynamic_lines = _dynamic_help_lines(resolved, raw_context=raw_context)
    if dynamic_lines:
        lines.extend(["", *dynamic_lines])

    lines.extend(["", "Parameters:", *_format_parameter_lines(params)])
    additional = params.get("additionalProperties") if isinstance(params, Mapping) else None
    if additional is False:
        lines.append("- Additional properties are not accepted.")

    if isinstance(capabilities, Mapping) and capabilities:
        cap_items = []
        if capabilities.get("supports_streaming"):
            cap_items.append("supports streaming")
        if capabilities.get("supports_cancellation"):
            cap_items.append("supports cancellation")
        for key, value in capabilities.items():
            if key in {"supports_streaming", "supports_cancellation"}:
                continue
            cap_items.append(f"{key}={value}")
        if cap_items:
            lines.extend(["", "Capabilities:", f"- {', '.join(cap_items)}"])

    if not available:
        lines.extend(["", "Availability note:", f"- This tool is {status}."])

    if include_schema:
        try:
            spec_text = json.dumps(spec, indent=2, sort_keys=True)
        except Exception:
            spec_text = repr(spec)
        lines.extend(["", "Spec (sent to LLM):", spec_text])

    return ToolHelpRenderResult("\n".join(lines).strip(), found=True, tool_name=resolved)


def render_tool_help_request(
    args: Mapping[str, Any] | None,
    *,
    registry: Any | None = None,
    entries: Mapping[str, Mapping[str, Any]] | None = None,
    db: Any = None,
    thread_id: str | None = None,
    raw_context: Mapping[str, Any] | None = None,
    default_include_schema: bool = False,
    default_include_unavailable: bool = False,
) -> ToolHelpRenderResult:
    """Parse tool/help arguments and render the shared help text."""

    args = args if isinstance(args, Mapping) else {}
    name = args.get("tool_name") or args.get("tool") or args.get("name") or args.get("_arg")
    include_schema = _bool_arg(args.get("include_schema"), default=default_include_schema)
    include_unavailable = _bool_arg(args.get("include_unavailable"), default=default_include_unavailable)
    return render_tool_help(
        str(name) if name is not None else None,
        registry=registry,
        entries=entries,
        db=db,
        thread_id=thread_id,
        raw_context=raw_context,
        include_schema=include_schema,
        include_unavailable=include_unavailable,
    )


__all__ = [
    "BUILTIN_TOOL_HELP_DETAILS",
    "HELP_TOOL_NAME",
    "ToolHelpRenderResult",
    "collect_tool_entries",
    "missing_detailed_tool_help_entries",
    "render_tool_help",
    "render_tool_help_request",
]
