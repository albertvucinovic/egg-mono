"""Tests for commands/tools.py ToolCommandsMixin."""
from __future__ import annotations

import json

import pytest


class TestToolsAdminCommands:
    """Tests for tools-admin commands through CommandRegistry dispatch."""

    def test_toggle_auto_approval_enables_when_disabled(self, egg_app, monkeypatch):
        """Should enable auto-approval when currently disabled."""
        approved = []

        def mock_approve(db, tid, decision, reason=None, tool_call_id=None):
            approved.append(decision)

        monkeypatch.setattr("eggthreads.approve_tool_calls_for_thread", mock_approve)

        egg_app.handle_command("/toggleAutoApproval")

        assert "global_approval" in approved or "revoke_global_approval" in approved

    def test_toggle_auto_approval_logs_status_change(self, egg_app, monkeypatch):
        """Should log the status change."""
        monkeypatch.setattr("eggthreads.approve_tool_calls_for_thread", lambda *a, **k: None)

        egg_app.handle_command("/toggleAutoApproval")

        assert any("auto-approval" in msg.lower() or "enabled" in msg.lower() or "disabled" in msg.lower()
                   for msg in egg_app._system_log)

    def test_tools_on_enables_tools_for_thread(self, egg_app, monkeypatch):
        """Should call set_thread_tools_enabled(True)."""
        enabled = []

        def mock_set(db, tid, value):
            enabled.append((tid, value))

        monkeypatch.setattr("eggthreads.set_thread_tools_enabled", mock_set)

        egg_app.handle_command("/toolsOn")

        assert len(enabled) == 1
        assert enabled[0][1] is True

    def test_tools_on_logs_success(self, egg_app, monkeypatch):
        """Should log success message."""
        monkeypatch.setattr("eggthreads.set_thread_tools_enabled", lambda *a: None)

        egg_app.handle_command("/toolsOn")

        assert any("enabled" in msg.lower() for msg in egg_app._system_log)

    def test_tools_off_disables_tools_for_thread(self, egg_app, monkeypatch):
        """Should call set_thread_tools_enabled(False)."""
        disabled = []

        def mock_set(db, tid, value):
            disabled.append((tid, value))

        monkeypatch.setattr("eggthreads.set_thread_tools_enabled", mock_set)

        egg_app.handle_command("/toolsOff")

        assert len(disabled) == 1
        assert disabled[0][1] is False

    def test_tools_off_logs_success(self, egg_app, monkeypatch):
        """Should log success message."""
        monkeypatch.setattr("eggthreads.set_thread_tools_enabled", lambda *a: None)

        egg_app.handle_command("/toolsOff")

        assert any("disabled" in msg.lower() for msg in egg_app._system_log)

    def test_disable_tool_disables_specific_tool(self, egg_app, monkeypatch):
        """Should disable named tool."""
        disabled = []

        def mock_disable(db, tid, name):
            disabled.append((tid, name))

        monkeypatch.setattr("eggthreads.disable_tool_for_thread", mock_disable)

        egg_app.handle_command("/disableTool bash")

        assert len(disabled) == 1
        assert disabled[0][1] == "bash"

    def test_disable_tool_requires_tool_name(self, egg_app):
        """Should show usage when no name given."""
        egg_app.handle_command("/disableTool")

        assert any("Usage" in msg or "usage" in msg.lower() for msg in egg_app._system_log)

    def test_disable_tool_logs_success(self, egg_app, monkeypatch):
        """Should log success message."""
        monkeypatch.setattr("eggthreads.disable_tool_for_thread", lambda *a: None)

        egg_app.handle_command("/disableTool bash")

        assert any("disabled" in msg.lower() or "bash" in msg.lower() for msg in egg_app._system_log)

    def test_enable_tool_enables_specific_tool(self, egg_app, monkeypatch):
        """Should enable named tool."""
        enabled = []

        def mock_enable(db, tid, name):
            enabled.append((tid, name))

        monkeypatch.setattr("eggthreads.enable_tool_for_thread", mock_enable)

        egg_app.handle_command("/enableTool bash")

        assert len(enabled) == 1
        assert enabled[0][1] == "bash"

    def test_enable_tool_requires_tool_name(self, egg_app):
        """Should show usage when no name given."""
        egg_app.handle_command("/enableTool")

        assert any("Usage" in msg or "usage" in msg.lower() for msg in egg_app._system_log)

    def test_tools_status_displays_tools_config(self, egg_app, monkeypatch, capsys):
        """Should display current tools configuration."""

        class MockConfig:
            llm_tools_enabled = True
            disabled_tools = ["python"]
            allow_raw_tool_output = False
            allowed_tools = None

        monkeypatch.setattr("eggthreads.get_thread_tools_config", lambda db, tid: MockConfig())

        egg_app.handle_command("/toolsStatus")

        captured = capsys.readouterr()
        assert "python" in captured.out.lower() or "DISABLED" in captured.out
        assert any("tools status" in msg.lower() for msg in egg_app._system_log)

    def test_tools_status_displays_allowlist_restricted_tools(self, egg_app, monkeypatch):
        """Should show allowlist-excluded tools as unavailable."""

        class MockConfig:
            llm_tools_enabled = True
            disabled_tools = []
            allow_raw_tool_output = False
            allowed_tools = {"bash"}

        printed = []
        monkeypatch.setattr("eggthreads.get_thread_tools_config", lambda db, tid: MockConfig())
        monkeypatch.setattr(
            "eggthreads.command_catalog._get_available_tools",
            lambda: {
                "bash": {"spec": {}, "local_only": False},
                "python": {"spec": {}, "local_only": False},
            },
        )
        monkeypatch.setattr(
            egg_app,
            "console_print_block",
            lambda title, text, **kwargs: printed.append((title, text)),
        )

        egg_app.handle_command("/toolsStatus")

        assert printed
        text = printed[0][1]
        assert "Tool allowlist: bash" in text
        assert "bash: enabled" in text
        assert "python: not allowed" in text

    def test_tools_secrets_enables_raw_mode(self, egg_app, monkeypatch):
        """Should enable raw mode on 'on'."""
        set_values = []

        def mock_set(db, tid, value):
            set_values.append(value)

        monkeypatch.setattr("eggthreads.set_thread_allow_raw_tool_output", mock_set)

        egg_app.handle_command("/toolsSecrets on")

        assert True in set_values

    def test_tools_secrets_disables_raw_mode(self, egg_app, monkeypatch):
        """Should disable raw mode on 'off'."""
        set_values = []

        def mock_set(db, tid, value):
            set_values.append(value)

        monkeypatch.setattr("eggthreads.set_thread_allow_raw_tool_output", mock_set)

        egg_app.handle_command("/toolsSecrets off")

        assert False in set_values

    def test_tools_secrets_shows_usage_for_invalid(self, egg_app):
        """Should show usage for invalid argument."""
        egg_app.handle_command("/toolsSecrets invalid")

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
        monkeypatch.setattr("egg.commands.tools.append_message", mock_append)
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
        import egg.commands.tools as tools_mod
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
        monkeypatch.setattr("egg.commands.tools.append_message", mock_append)
        monkeypatch.setattr("eggthreads.approve_tool_calls_for_thread", lambda *a, **k: None)
        monkeypatch.setattr("eggthreads.create_snapshot", lambda *a: None)

        egg_app.enqueue_bash_tool("echo hello", hidden=True)

        assert messages[0][2].get("no_api") is True

    def test_logs_enqueue_message(self, egg_app, monkeypatch):
        """Should log message about queued command."""
        monkeypatch.setattr("egg.commands.tools.append_message", lambda *a, **k: "msg_id")
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
