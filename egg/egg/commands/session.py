"""Session/REPL-related command mixins for the egg application."""
from __future__ import annotations

from eggthreads import parse_args


class SessionCommandsMixin:
    """Mixin providing persistent session and REPL commands."""

    def cmd_sessionStatus(self, arg: str) -> None:
        try:
            from eggthreads import get_thread_session_status, find_runtime_thread
            lines = []
            st = get_thread_session_status(self.db, self.current_thread)
            lines.append("Current thread session:")
            lines.append(f"  Enabled: {st.enabled}")
            lines.append(f"  Provider: {st.provider}")
            lines.append(f"  Session ID: {st.session_id or '(none)'}")
            lines.append(f"  Status: {st.status}")
            lines.append(f"  Share REPL channel: {getattr(st, 'share_repl', False)}")
            if st.container_name:
                lines.append(f"  Container: {st.container_name}")
            if st.message:
                lines.append(f"  Message: {st.message}")
            for language in ("python", "bash"):
                rt = find_runtime_thread(self.db, self.current_thread, language=language)
                if rt is not None:
                    rst = get_thread_session_status(self.db, rt.runtime_thread_id)
                    lines.append("")
                    lines.append(f"Runtime {language} ({rt.runtime_thread_id[-8:]}):")
                    lines.append(f"  Session ID: {rst.session_id or '(none)'}")
                    lines.append(f"  Provider: {rst.provider}")
                    lines.append(f"  Status: {rst.status}")
                    lines.append(f"  Share REPL channel: {getattr(rst, 'share_repl', False)}")
                    if rst.container_name:
                        lines.append(f"  Container: {rst.container_name}")
            text = "\n".join(lines)
            self.log_system("Session status (see console for full).")
            self.console_print_block("Session Status", text, border_style="magenta")
        except Exception as e:
            self.log_system(f"/sessionStatus error: {e}")

    def cmd_sessionOn(self, arg: str) -> None:
        parsed = parse_args(arg or "")
        provider = parsed.get("provider") or parsed.positional_or(0, "docker") or "docker"
        image = parsed.get("image") or "egg-rlm-session"
        share_raw = parsed.get("share_with_children", parsed.get("share", "false")) or "false"
        share = str(share_raw).strip().lower() in ("1", "true", "yes", "on")
        share_repl_raw = parsed.get("share_repl", "false") or "false"
        share_repl = str(share_repl_raw).strip().lower() in ("1", "true", "yes", "on")
        try:
            from eggthreads import enable_thread_session, get_thread_session_status
            sid = enable_thread_session(
                self.db,
                self.current_thread,
                provider=provider,
                image=image,
                share_with_children_default=share,
                share_repl=share_repl,
                reason="/sessionOn",
            )
            st = get_thread_session_status(self.db, self.current_thread)
            self.log_system(f"Session enabled: provider={provider} session={sid[-8:] if sid else '(none)'} status={st.status}")
        except Exception as e:
            self.log_system(f"/sessionOn error: {e}")

    def cmd_sessionOff(self, arg: str) -> None:
        try:
            from eggthreads import disable_thread_session
            disable_thread_session(self.db, self.current_thread, reason="/sessionOff")
            self.log_system("Session disabled for this thread.")
        except Exception as e:
            self.log_system(f"/sessionOff error: {e}")

    def cmd_sessionStop(self, arg: str) -> None:
        parsed = parse_args(arg or "")
        language = parsed.get("language") or parsed.positional_or(0, "") or ""
        try:
            from eggthreads import find_runtime_thread, stop_thread_session
            targets = []
            if language in ("python", "bash"):
                rt = find_runtime_thread(self.db, self.current_thread, language=language)
                targets = [rt.runtime_thread_id] if rt is not None else [self.current_thread]
            elif language in ("all", "runtime", "runtimes"):
                for lang in ("python", "bash"):
                    rt = find_runtime_thread(self.db, self.current_thread, language=lang)
                    if rt is not None:
                        targets.append(rt.runtime_thread_id)
                if not targets:
                    targets = [self.current_thread]
            else:
                targets = [self.current_thread]
            statuses = [stop_thread_session(self.db, target, reason="/sessionStop") for target in targets]
            summary = ", ".join(f"{st.session_id or '(none)'}:{st.status}" for st in statuses)
            self.log_system(f"Session stop requested: {summary}")
        except Exception as e:
            self.log_system(f"/sessionStop error: {e}")

    def cmd_sessionReset(self, arg: str) -> None:
        parsed = parse_args(arg or "")
        language = parsed.get("language") or parsed.positional_or(0, "") or ""
        try:
            from eggthreads import find_runtime_thread, reset_thread_session
            targets = []
            if language in ("python", "bash"):
                rt = find_runtime_thread(self.db, self.current_thread, language=language)
                targets = [rt.runtime_thread_id] if rt is not None else [self.current_thread]
            elif language in ("all", "runtime", "runtimes"):
                for lang in ("python", "bash"):
                    rt = find_runtime_thread(self.db, self.current_thread, language=lang)
                    if rt is not None:
                        targets.append(rt.runtime_thread_id)
                if not targets:
                    targets = [self.current_thread]
            else:
                targets = [self.current_thread]
            sids = [reset_thread_session(self.db, target, reason="/sessionReset") for target in targets]
            self.log_system(f"Session reset: {', '.join(sid[-8:] for sid in sids if sid) or '(none)'}")
        except Exception as e:
            self.log_system(f"/sessionReset error: {e}")

    def cmd_sessionCleanup(self, arg: str) -> None:
        parsed = parse_args(arg or "")
        mode = (parsed.positional_or(0, "stopped") or "stopped").strip().lower()
        stopped_only = mode not in ("all", "force")
        older_than = parsed.get("older_than") or parsed.get("olderThan")
        try:
            from eggthreads import cleanup_docker_sessions
            from eggthreads.session import _parse_duration_seconds  # type: ignore

            removed = cleanup_docker_sessions(
                self.db,
                stopped_only=stopped_only,
                older_than_sec=_parse_duration_seconds(older_than),
            )
            if not removed:
                self.log_system("No matching Docker RLM session containers to clean up.")
                return
            lines = []
            for item in removed:
                status = "removed" if item.get("removed") else f"error: {item.get('error', 'unknown')}"
                lines.append(f"{item.get('name')}: {status}")
            text = "\n".join(lines)
            self.log_system(f"Session cleanup processed {len(removed)} container(s) (see console for details).")
            self.console_print_block("Session Cleanup", text, border_style="magenta")
        except Exception as e:
            self.log_system(f"/sessionCleanup error: {e}")

    def cmd_pythonRepl(self, arg: str) -> None:
        code = arg or ""
        if not code.strip():
            self.log_system("Usage: /pythonRepl <python code>")
            return
        try:
            from eggthreads import enqueue_user_tool_call, create_snapshot
            tcid = enqueue_user_tool_call(
                self.db,
                self.current_thread,
                "python_repl",
                {"code": code},
                content=f"/pythonRepl {code}",
                hidden=True,
                keep_user_turn=True,
                origin="ui_python_repl",
                auto_approve=True,
                approval_reason="Approved /pythonRepl command",
            )
            create_snapshot(self.db, self.current_thread)
            self.log_system(f"Python REPL queued as tool call {tcid[-8:]}; scheduler will execute it.")
            self.ensure_scheduler_for(self.current_thread)
        except Exception as e:
            self.log_system(f"/pythonRepl error: {e}")

    def cmd_bashRepl(self, arg: str) -> None:
        script = arg or ""
        if not script.strip():
            self.log_system("Usage: /bashRepl <bash script>")
            return
        try:
            from eggthreads import enqueue_user_tool_call, create_snapshot
            tcid = enqueue_user_tool_call(
                self.db,
                self.current_thread,
                "bash_repl",
                {"script": script},
                content=f"/bashRepl {script}",
                hidden=True,
                keep_user_turn=True,
                origin="ui_bash_repl",
                auto_approve=True,
                approval_reason="Approved /bashRepl command",
            )
            create_snapshot(self.db, self.current_thread)
            self.log_system(f"Bash REPL queued as tool call {tcid[-8:]}; scheduler will execute it.")
            self.ensure_scheduler_for(self.current_thread)
        except Exception as e:
            self.log_system(f"/bashRepl error: {e}")
