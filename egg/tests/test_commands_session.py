"""Tests for commands/session.py SessionCommandsMixin."""
from __future__ import annotations

import pytest

from egg.commands.session import SessionCommandsMixin


class DummySessionApp(SessionCommandsMixin):
    def __init__(self):
        from eggthreads import ThreadsDB, create_root_thread
        self.db = ThreadsDB(":memory:")
        self.db.init_schema()
        self.current_thread = create_root_thread(self.db, name="root")
        self._system_log = []
        self.printed = []
        self.ensured = []

    def log_system(self, message: str) -> None:
        self._system_log.append(message)

    def console_print_block(self, title: str, text: str, **kwargs) -> None:
        self.printed.append((title, text, kwargs))

    def ensure_scheduler_for(self, tid: str) -> None:
        self.ensured.append(tid)


@pytest.fixture
def app(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    return DummySessionApp()


class TestSessionCommands:
    def test_session_on_enables_session(self, app, monkeypatch):
        calls = []
        monkeypatch.setattr("eggthreads.enable_thread_session", lambda db, tid, **kw: calls.append(kw) or "sess_test")
        class Status:
            status = "available"
        monkeypatch.setattr("eggthreads.get_thread_session_status", lambda db, tid: Status())

        app.cmd_sessionOn("provider=memory")

        assert calls
        assert calls[0]["provider"] == "memory"
        assert any("Session enabled" in msg for msg in app._system_log)

    def test_session_on_parses_share_repl(self, app, monkeypatch):
        calls = []
        monkeypatch.setattr("eggthreads.enable_thread_session", lambda db, tid, **kw: calls.append(kw) or "sess_test")
        class Status:
            status = "available"
        monkeypatch.setattr("eggthreads.get_thread_session_status", lambda db, tid: Status())

        app.cmd_sessionOn("provider=memory share_repl=true")

        assert calls
        assert calls[0]["share_repl"] is True

    def test_session_off_disables_session(self, app, monkeypatch):
        calls = []
        monkeypatch.setattr("eggthreads.disable_thread_session", lambda db, tid, reason=None: calls.append((tid, reason)))

        app.cmd_sessionOff("")

        assert calls
        assert calls[0][1] == "/sessionOff"

    def test_session_stop_stops_current_session(self, app, monkeypatch):
        calls = []
        class Status:
            session_id = "sess_test"
            status = "stopped"
        monkeypatch.setattr("eggthreads.stop_thread_session", lambda db, tid, reason=None: calls.append((tid, reason)) or Status())
        monkeypatch.setattr("eggthreads.find_runtime_thread", lambda *a, **k: None)

        app.cmd_sessionStop("")

        assert calls == [(app.current_thread, "/sessionStop")]

    def test_session_reset_resets_current_session(self, app, monkeypatch):
        calls = []
        monkeypatch.setattr("eggthreads.reset_thread_session", lambda db, tid, reason=None: calls.append((tid, reason)) or "sess_new")
        monkeypatch.setattr("eggthreads.find_runtime_thread", lambda *a, **k: None)

        app.cmd_sessionReset("")

        assert calls == [(app.current_thread, "/sessionReset")]

    def test_python_repl_executes(self, app, monkeypatch):
        calls = []
        monkeypatch.setattr("eggthreads.enqueue_user_tool_call", lambda db, tid, name, args, **kw: calls.append((tid, name, args, kw)) or "tc_python")
        monkeypatch.setattr("eggthreads.create_snapshot", lambda db, tid: None)

        app.cmd_pythonRepl("print('hi')")

        assert calls
        assert calls[0][1] == "python_repl"
        assert calls[0][2] == {"code": "print('hi')"}
        assert calls[0][3]["origin"] == "ui_python_repl"
        assert app.ensured == [app.current_thread]

    def test_bash_repl_executes(self, app, monkeypatch):
        calls = []
        monkeypatch.setattr("eggthreads.enqueue_user_tool_call", lambda db, tid, name, args, **kw: calls.append((tid, name, args, kw)) or "tc_bash")
        monkeypatch.setattr("eggthreads.create_snapshot", lambda db, tid: None)

        app.cmd_bashRepl("echo hi")

        assert calls
        assert calls[0][1] == "bash_repl"
        assert calls[0][2] == {"script": "echo hi"}
        assert calls[0][3]["origin"] == "ui_bash_repl"
        assert app.ensured == [app.current_thread]

    def test_session_status_prints(self, app, monkeypatch):
        class Status:
            enabled = True
            provider = "memory"
            session_id = "sess_test"
            status = "available"
            message = "ok"
            container_name = None
        monkeypatch.setattr("eggthreads.get_thread_session_status", lambda db, tid: Status())
        monkeypatch.setattr("eggthreads.find_runtime_thread", lambda *a, **k: None)

        app.cmd_sessionStatus("")

        assert app.printed
        assert "Session" in app.printed[0][0]
