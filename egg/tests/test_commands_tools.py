"""Tests for commands/tools.py ToolCommandsMixin."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Ensure project root is in path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class TestCmdToggleAutoApproval:
    """Tests for cmd_toggleAutoApproval()."""

    def test_enables_when_disabled(self, egg_app, monkeypatch):
        """Should enable auto-approval when currently disabled."""
        approved = []
        def mock_approve(db, tid, decision, reason=None, tool_call_id=None):
            approved.append(decision)
        # Mock at commands.tools level since it's imported there
        import commands.tools as tools_mod
        monkeypatch.setattr(tools_mod, "approve_tool_calls_for_thread", mock_approve)

        egg_app.cmd_toggleAutoApproval("")

        assert "global_approval" in approved or "revoke_global_approval" in approved

    def test_logs_status_change(self, egg_app, monkeypatch):
        """Should log the status change."""
        import commands.tools as tools_mod
        monkeypatch.setattr(tools_mod, "approve_tool_calls_for_thread", lambda *a, **k: None)

        egg_app.cmd_toggleAutoApproval("")

        assert any("auto-approval" in msg.lower() or "enabled" in msg.lower() or "disabled" in msg.lower()
                   for msg in egg_app._system_log)


class TestCmdToolsOn:
    """Tests for cmd_toolsOn()."""

    def test_enables_tools_for_thread(self, egg_app, monkeypatch):
        """Should call set_thread_tools_enabled(True)."""
        enabled = []
        def mock_set(db, tid, value):
            enabled.append((tid, value))
        monkeypatch.setattr("eggthreads.set_thread_tools_enabled", mock_set)

        egg_app.cmd_toolsOn("")

        assert len(enabled) == 1
        assert enabled[0][1] is True

    def test_logs_success(self, egg_app, monkeypatch):
        """Should log success message."""
        monkeypatch.setattr("eggthreads.set_thread_tools_enabled", lambda *a: None)

        egg_app.cmd_toolsOn("")

        assert any("enabled" in msg.lower() for msg in egg_app._system_log)


class TestCmdToolsOff:
    """Tests for cmd_toolsOff()."""

    def test_disables_tools_for_thread(self, egg_app, monkeypatch):
        """Should call set_thread_tools_enabled(False)."""
        disabled = []
        def mock_set(db, tid, value):
            disabled.append((tid, value))
        monkeypatch.setattr("eggthreads.set_thread_tools_enabled", mock_set)

        egg_app.cmd_toolsOff("")

        assert len(disabled) == 1
        assert disabled[0][1] is False

    def test_logs_success(self, egg_app, monkeypatch):
        """Should log success message."""
        monkeypatch.setattr("eggthreads.set_thread_tools_enabled", lambda *a: None)

        egg_app.cmd_toolsOff("")

        assert any("disabled" in msg.lower() for msg in egg_app._system_log)


class TestCmdDisableTool:
    """Tests for cmd_disableTool()."""

    def test_disables_specific_tool(self, egg_app, monkeypatch):
        """Should disable named tool."""
        disabled = []
        def mock_disable(db, tid, name):
            disabled.append((tid, name))
        monkeypatch.setattr("eggthreads.disable_tool_for_thread", mock_disable)

        egg_app.cmd_disableTool("bash")

        assert len(disabled) == 1
        assert disabled[0][1] == "bash"

    def test_requires_tool_name(self, egg_app):
        """Should show usage when no name given."""
        egg_app.cmd_disableTool("")

        assert any("Usage" in msg or "usage" in msg.lower() for msg in egg_app._system_log)

    def test_logs_success(self, egg_app, monkeypatch):
        """Should log success message."""
        monkeypatch.setattr("eggthreads.disable_tool_for_thread", lambda *a: None)

        egg_app.cmd_disableTool("bash")

        assert any("disabled" in msg.lower() or "bash" in msg.lower() for msg in egg_app._system_log)


class TestCmdEnableTool:
    """Tests for cmd_enableTool()."""

    def test_enables_specific_tool(self, egg_app, monkeypatch):
        """Should enable named tool."""
        enabled = []
        def mock_enable(db, tid, name):
            enabled.append((tid, name))
        monkeypatch.setattr("eggthreads.enable_tool_for_thread", mock_enable)

        egg_app.cmd_enableTool("bash")

        assert len(enabled) == 1
        assert enabled[0][1] == "bash"

    def test_requires_tool_name(self, egg_app):
        """Should show usage when no name given."""
        egg_app.cmd_enableTool("")

        assert any("Usage" in msg or "usage" in msg.lower() for msg in egg_app._system_log)


class TestCmdToolsStatus:
    """Tests for cmd_toolsStatus()."""

    def test_displays_tools_config(self, egg_app, monkeypatch):
        """Should display current tools configuration."""
        class MockConfig:
            llm_tools_enabled = True
            disabled_tools = ["python"]
            allow_raw_tool_output = False

        monkeypatch.setattr("eggthreads.get_thread_tools_config", lambda db, tid: MockConfig())

        egg_app.cmd_toolsStatus("")

        assert any("enabled" in msg.lower() or "python" in msg.lower() for msg in egg_app._system_log)


class TestCmdToolsSecrets:
    """Tests for cmd_toolsSecrets()."""

    def test_enables_raw_mode(self, egg_app, monkeypatch):
        """Should enable raw mode on 'on'."""
        set_values = []
        def mock_set(db, tid, value):
            set_values.append(value)
        monkeypatch.setattr("eggthreads.set_thread_allow_raw_tool_output", mock_set)

        egg_app.cmd_toolsSecrets("on")

        assert True in set_values

    def test_disables_raw_mode(self, egg_app, monkeypatch):
        """Should disable raw mode on 'off'."""
        set_values = []
        def mock_set(db, tid, value):
            set_values.append(value)
        monkeypatch.setattr("eggthreads.set_thread_allow_raw_tool_output", mock_set)

        egg_app.cmd_toolsSecrets("off")

        assert False in set_values

    def test_shows_usage_for_invalid(self, egg_app):
        """Should show usage for invalid argument."""
        egg_app.cmd_toolsSecrets("invalid")

        assert any("Usage" in msg or "usage" in msg.lower() for msg in egg_app._system_log)


class TestCmdSchedulers:
    """Tests for cmd_schedulers()."""

    def test_shows_no_schedulers_message(self, egg_app):
        """Should show message when no schedulers."""
        egg_app.active_schedulers = {}

        egg_app.cmd_schedulers("")

        assert any("No active" in msg or "no active" in msg.lower() for msg in egg_app._system_log)

    def test_lists_active_schedulers(self, egg_app):
        """Should list active schedulers."""
        egg_app.active_schedulers = {
            egg_app.current_thread: {"scheduler": None, "task": None}
        }

        egg_app.cmd_schedulers("")

        assert any("scheduler" in msg.lower() or egg_app.current_thread[-8:] in msg
                   for msg in egg_app._system_log)


class TestEnqueueBashTool:
    """Tests for enqueue_bash_tool()."""

    def test_creates_tool_call_message(self, egg_app, monkeypatch):
        """Should create user message with tool_calls."""
        messages = []
        original_append = egg_app.db.conn.execute

        # Track appended messages
        from eggthreads import append_message
        def mock_append(db, tid, role, content, extra=None):
            messages.append((role, content, extra))
            return "msg_id"
        monkeypatch.setattr("commands.tools.append_message", mock_append)
        monkeypatch.setattr("eggthreads.approve_tool_calls_for_thread", lambda *a, **k: None)
        monkeypatch.setattr("eggthreads.create_snapshot", lambda *a: None)

        egg_app.enqueue_bash_tool("echo hello", hidden=False)

        # Should have created a message
        assert len(messages) == 1
        assert messages[0][0] == "user"
        assert "tool_calls" in messages[0][2]

    def test_auto_approves_tool_call(self, egg_app, monkeypatch):
        """Should approve the tool call automatically."""
        approved = []
        def mock_approve(db, tid, decision, reason=None, tool_call_id=None):
            approved.append((decision, tool_call_id))
        # Mock at commands.tools level since it's imported there
        import commands.tools as tools_mod
        monkeypatch.setattr(tools_mod, "approve_tool_calls_for_thread", mock_approve)
        monkeypatch.setattr(tools_mod, "append_message", lambda *a, **k: "msg_id")
        monkeypatch.setattr(tools_mod, "create_snapshot", lambda *a: None)

        egg_app.enqueue_bash_tool("echo hello", hidden=False)

        assert len(approved) == 1
        assert approved[0][0] == "granted"

    def test_hidden_mode_sets_no_api(self, egg_app, monkeypatch):
        """Should set no_api=True for $$ commands."""
        messages = []
        def mock_append(db, tid, role, content, extra=None):
            messages.append((role, content, extra))
            return "msg_id"
        monkeypatch.setattr("commands.tools.append_message", mock_append)
        monkeypatch.setattr("eggthreads.approve_tool_calls_for_thread", lambda *a, **k: None)
        monkeypatch.setattr("eggthreads.create_snapshot", lambda *a: None)

        egg_app.enqueue_bash_tool("echo hello", hidden=True)

        assert messages[0][2].get("no_api") is True

    def test_logs_enqueue_message(self, egg_app, monkeypatch):
        """Should log message about queued command."""
        monkeypatch.setattr("commands.tools.append_message", lambda *a, **k: "msg_id")
        monkeypatch.setattr("eggthreads.approve_tool_calls_for_thread", lambda *a, **k: None)
        monkeypatch.setattr("eggthreads.create_snapshot", lambda *a: None)

        egg_app.enqueue_bash_tool("echo hello", hidden=False)

        assert any("Queued" in msg or "queued" in msg.lower() or "bash" in msg.lower()
                   for msg in egg_app._system_log)

    def test_skips_empty_command(self, egg_app):
        """Should skip empty bash command."""
        egg_app.enqueue_bash_tool("", hidden=False)

        assert any("Empty" in msg or "empty" in msg.lower() or "skipping" in msg.lower()
                   for msg in egg_app._system_log)
