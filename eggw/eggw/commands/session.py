"""Session/REPL commands for eggw backend."""
from __future__ import annotations

from typing import List

from eggthreads import (
    parse_args,
    enable_thread_session,
    disable_thread_session,
    get_thread_session_status,
    find_runtime_thread,
    stop_thread_session,
    reset_thread_session,
    execute_python_repl,
    execute_bash_repl,
)

from ..models import CommandResponse
from .. import core
from ..core import ensure_scheduler_for


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _status_lines(thread_id: str) -> tuple[list[str], dict]:
    st = get_thread_session_status(core.db, thread_id)
    lines = [
        "Current thread session:",
        f"  Enabled: {st.enabled}",
        f"  Provider: {st.provider}",
        f"  Session ID: {st.session_id or '(none)'}",
        f"  Status: {st.status}",
        f"  Share REPL channel: {getattr(st, 'share_repl', False)}",
    ]
    if st.container_name:
        lines.append(f"  Container: {st.container_name}")
    if st.message:
        lines.append(f"  Message: {st.message}")

    runtimes = []
    for language in ("python", "bash"):
        rt = find_runtime_thread(core.db, thread_id, language=language)
        if rt is None:
            continue
        rst = get_thread_session_status(core.db, rt.runtime_thread_id)
        runtimes.append({
            "language": language,
            "runtime_thread_id": rt.runtime_thread_id,
            "session_id": rst.session_id,
            "provider": rst.provider,
            "status": rst.status,
            "container_name": rst.container_name,
            "share_repl": getattr(rst, "share_repl", False),
        })
        lines.append("")
        lines.append(f"Runtime {language} ({rt.runtime_thread_id[-8:]}):")
        lines.append(f"  Session ID: {rst.session_id or '(none)'}")
        lines.append(f"  Provider: {rst.provider}")
        lines.append(f"  Status: {rst.status}")
        lines.append(f"  Share REPL channel: {getattr(rst, 'share_repl', False)}")
        if rst.container_name:
            lines.append(f"  Container: {rst.container_name}")
    data = {
        "enabled": st.enabled,
        "provider": st.provider,
        "session_id": st.session_id,
        "status": st.status,
        "container_name": st.container_name,
        "share_repl": getattr(st, "share_repl", False),
        "runtimes": runtimes,
    }
    return lines, data


def _target_threads(thread_id: str, language: str) -> List[str]:
    lang = (language or "").strip().lower()
    if lang in ("python", "bash"):
        rt = find_runtime_thread(core.db, thread_id, language=lang)
        return [rt.runtime_thread_id] if rt is not None else [thread_id]
    if lang in ("all", "runtime", "runtimes"):
        targets: List[str] = []
        for candidate in ("python", "bash"):
            rt = find_runtime_thread(core.db, thread_id, language=candidate)
            if rt is not None:
                targets.append(rt.runtime_thread_id)
        return targets or [thread_id]
    return [thread_id]


async def cmd_session_status(thread_id: str) -> CommandResponse:
    try:
        lines, data = _status_lines(thread_id)
        return CommandResponse(success=True, message="\n".join(lines), data=data)
    except Exception as e:
        return CommandResponse(success=False, message=f"/sessionStatus error: {e}")


async def cmd_session_on(thread_id: str, arg: str) -> CommandResponse:
    parsed = parse_args(arg or "")
    provider = parsed.get("provider") or parsed.positional_or(0, "docker") or "docker"
    image = parsed.get("image") or "egg-rlm-session"
    share_children = _truthy(parsed.get("share_with_children", parsed.get("share", "false")))
    share_repl = _truthy(parsed.get("share_repl", "false"))
    try:
        sid = enable_thread_session(
            core.db,
            thread_id,
            provider=provider,
            image=image,
            share_with_children_default=share_children,
            share_repl=share_repl,
            reason="/sessionOn:web",
        )
        st = get_thread_session_status(core.db, thread_id)
        return CommandResponse(
            success=True,
            message=f"Session enabled: provider={provider} session={sid[-8:] if sid else '(none)'} status={st.status}",
            data={"session_id": sid, "provider": provider, "status": st.status, "share_repl": share_repl},
        )
    except Exception as e:
        return CommandResponse(success=False, message=f"/sessionOn error: {e}")


async def cmd_session_off(thread_id: str) -> CommandResponse:
    try:
        disable_thread_session(core.db, thread_id, reason="/sessionOff:web")
        return CommandResponse(success=True, message="Session disabled for this thread.")
    except Exception as e:
        return CommandResponse(success=False, message=f"/sessionOff error: {e}")


async def cmd_session_stop(thread_id: str, arg: str) -> CommandResponse:
    parsed = parse_args(arg or "")
    language = parsed.get("language") or parsed.positional_or(0, "") or ""
    try:
        statuses = []
        for target in _target_threads(thread_id, language):
            st = stop_thread_session(core.db, target, reason="/sessionStop:web")
            statuses.append({"thread_id": target, "session_id": st.session_id, "status": st.status})
        msg = ", ".join(f"{s['thread_id'][-8:]}:{s['status']}" for s in statuses)
        return CommandResponse(success=True, message=f"Session stop requested: {msg}", data={"statuses": statuses})
    except Exception as e:
        return CommandResponse(success=False, message=f"/sessionStop error: {e}")


async def cmd_session_reset(thread_id: str, arg: str) -> CommandResponse:
    parsed = parse_args(arg or "")
    language = parsed.get("language") or parsed.positional_or(0, "") or ""
    try:
        resets = []
        for target in _target_threads(thread_id, language):
            sid = reset_thread_session(core.db, target, reason="/sessionReset:web")
            resets.append({"thread_id": target, "session_id": sid})
        msg = ", ".join(f"{r['thread_id'][-8:]}:{r['session_id'][-8:]}" for r in resets if r.get("session_id")) or "(none)"
        return CommandResponse(success=True, message=f"Session reset: {msg}", data={"resets": resets})
    except Exception as e:
        return CommandResponse(success=False, message=f"/sessionReset error: {e}")


async def cmd_python_repl(thread_id: str, code: str) -> CommandResponse:
    if not (code or "").strip():
        return CommandResponse(success=False, message="Usage: /pythonRepl <python code>")
    try:
        out = execute_python_repl(core.db, thread_id, code, drive_runtime_tools=True)
        ensure_scheduler_for(thread_id)
        return CommandResponse(success=True, message=out, data={"language": "python"})
    except Exception as e:
        return CommandResponse(success=False, message=f"/pythonRepl error: {e}")


async def cmd_bash_repl(thread_id: str, script: str) -> CommandResponse:
    if not (script or "").strip():
        return CommandResponse(success=False, message="Usage: /bashRepl <bash script>")
    try:
        out = execute_bash_repl(core.db, thread_id, script, drive_runtime_tools=True)
        ensure_scheduler_for(thread_id)
        return CommandResponse(success=True, message=out, data={"language": "bash"})
    except Exception as e:
        return CommandResponse(success=False, message=f"/bashRepl error: {e}")
