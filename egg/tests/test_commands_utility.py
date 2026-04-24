"""Tests for commands/utility.py UtilityCommandsMixin."""
from __future__ import annotations

import pytest


class TestCmdHelp:
    """Tests for cmd_help()."""

    def test_displays_commands_text(self, egg_app):
        """Should display COMMANDS_TEXT in console."""
        egg_app.cmd_help("")

        # Should log help message
        assert any("Help" in msg or "help" in msg.lower() or "Command" in msg
                   for msg in egg_app._system_log)


class TestCmdPaste:
    """Tests for cmd_paste()."""

    def test_pastes_clipboard_to_input(self, egg_app, monkeypatch):
        """Should paste clipboard content to input."""
        import egg.commands.utility as util_mod
        monkeypatch.setattr(util_mod, "read_clipboard", lambda: "pasted content")

        egg_app.cmd_paste("")

        assert egg_app.input_panel.get_text() == "pasted content"

    def test_sanitizes_clipboard_terminal_controls(self, egg_app, monkeypatch):
        """Should not store terminal-control sequences from clipboard."""
        import egg.commands.utility as util_mod
        monkeypatch.setattr(util_mod, "read_clipboard", lambda: "a\x1b[2Jb\r\x08c")

        egg_app.cmd_paste("")

        text = egg_app.input_panel.editor.editor.get_text()
        assert "\x1b" not in text
        assert "\r" not in text
        assert "\x08" not in text

    def test_handles_empty_clipboard(self, egg_app, monkeypatch):
        """Should handle empty clipboard gracefully."""
        import egg.commands.utility as util_mod
        monkeypatch.setattr(util_mod, "read_clipboard", lambda: "")

        egg_app.cmd_paste("")

        # Actual message is "Clipboard is empty."
        assert any("Clipboard is empty" in msg for msg in egg_app._system_log)

    def test_handles_clipboard_failure(self, egg_app, monkeypatch):
        """Should handle clipboard failure gracefully."""
        import egg.commands.utility as util_mod
        monkeypatch.setattr(util_mod, "read_clipboard", lambda: None)

        egg_app.cmd_paste("")

        # Actual message is "Failed to read clipboard."
        assert any("Failed to read clipboard" in msg for msg in egg_app._system_log)

    def test_logs_paste_success(self, egg_app, monkeypatch):
        """Should log success with character count."""
        import egg.commands.utility as util_mod
        monkeypatch.setattr(util_mod, "read_clipboard", lambda: "test content")

        egg_app.cmd_paste("")

        assert any("Pasted" in msg or "characters" in msg for msg in egg_app._system_log)


class TestCmdQuit:
    """Tests for cmd_quit()."""

    def test_sets_running_false(self, egg_app):
        """Should set self.running = False."""
        egg_app.running = True

        egg_app.cmd_quit("")

        assert egg_app.running is False


class TestCmdEnterMode:
    """Tests for cmd_enterMode()."""

    def test_sets_send_mode(self, egg_app):
        """Should set enter_sends = True for 'send'."""
        egg_app.enter_sends = False

        egg_app.cmd_enterMode("send")

        assert egg_app.enter_sends is True

    def test_sets_newline_mode(self, egg_app):
        """Should set enter_sends = False for 'newline'."""
        egg_app.enter_sends = True

        egg_app.cmd_enterMode("newline")

        assert egg_app.enter_sends is False

    def test_accepts_short_forms(self, egg_app):
        """Should accept 's' for send and 'n' for newline."""
        egg_app.enter_sends = False
        egg_app.cmd_enterMode("s")
        assert egg_app.enter_sends is True

        egg_app.cmd_enterMode("n")
        assert egg_app.enter_sends is False

    def test_accepts_on_off(self, egg_app):
        """Should accept 'on' for send and 'off' for newline."""
        egg_app.enter_sends = False
        egg_app.cmd_enterMode("on")
        assert egg_app.enter_sends is True

        egg_app.cmd_enterMode("off")
        assert egg_app.enter_sends is False

    def test_shows_usage_for_invalid(self, egg_app):
        """Should show usage for invalid argument."""
        egg_app.cmd_enterMode("invalid")

        assert any("Usage" in msg or "usage" in msg.lower() for msg in egg_app._system_log)

    def test_logs_mode_change(self, egg_app):
        """Should log the mode change."""
        egg_app.cmd_enterMode("send")

        assert any("Enter mode" in msg or "enter mode" in msg.lower() or "send" in msg.lower()
                   for msg in egg_app._system_log)


class TestCmdCost:
    """Tests for cmd_cost()."""

    def test_displays_token_statistics(self, egg_app, monkeypatch):
        """Should display token usage from current_token_stats."""
        # Mock current_token_stats to return some values
        monkeypatch.setattr(
            egg_app, "current_token_stats",
            lambda: (1000, {"total_input_tokens": 500, "total_output_tokens": 200})
        )

        egg_app.cmd_cost("")

        assert any("token" in msg.lower() or "cost" in msg.lower() for msg in egg_app._system_log)

    def test_handles_no_stats_available(self, egg_app, monkeypatch):
        """Should handle case when no stats available."""
        monkeypatch.setattr(egg_app, "current_token_stats", lambda: (None, None))

        egg_app.cmd_cost("")

        assert any("No snapshot" in msg or "no snapshot" in msg.lower() or "not available" in msg.lower()
                   for msg in egg_app._system_log)

    def test_shows_per_model_breakdown(self, egg_app, monkeypatch):
        """Should show per-model breakdown when available."""
        monkeypatch.setattr(
            egg_app, "current_token_stats",
            lambda: (1000, {
                "total_input_tokens": 500,
                "total_output_tokens": 200,
                "by_model": {
                    "gpt-4": {"total_input_tokens": 300, "total_output_tokens": 100}
                },
                "cost_usd": {"total": 0.05}
            })
        )

        egg_app.cmd_cost("")

        # Should log cost information
        assert any("cost" in msg.lower() or "$" in msg or "token" in msg.lower()
                   for msg in egg_app._system_log)


class TestCmdStartSearxng:
    """Tests for cmd_startSearxng()."""

    def test_reports_missing_compose_binary(self, egg_app, monkeypatch):
        """Logs a helpful error when neither docker compose nor docker-compose is installed."""
        import egg.commands.utility as util_mod
        monkeypatch.setattr(util_mod, "_find_searxng_dir", lambda: util_mod.Path("/tmp/searxng"))
        # Simulate a searxng/docker-compose.yml that exists
        monkeypatch.setattr(util_mod, "_resolve_compose_cmd", lambda: None)

        egg_app.cmd_startSearxng("")

        assert any("neither 'docker compose' nor 'docker-compose'" in m
                   for m in egg_app._system_log)

    def test_reports_missing_searxng_dir(self, egg_app, monkeypatch):
        """Logs a helpful error when searxng/docker-compose.yml cannot be found."""
        import egg.commands.utility as util_mod
        monkeypatch.setattr(util_mod, "_find_searxng_dir", lambda: None)

        egg_app.cmd_startSearxng("")

        assert any("could not locate searxng" in m for m in egg_app._system_log)

    def test_invokes_compose_up_in_background(self, egg_app, monkeypatch, tmp_path):
        """Spawns a thread that runs `<compose> up -d` in the searxng dir."""
        import egg.commands.utility as util_mod

        compose_dir = tmp_path / "searxng"
        compose_dir.mkdir()
        (compose_dir / "docker-compose.yml").write_text("services: {}\n")

        monkeypatch.setattr(util_mod, "_find_searxng_dir", lambda: compose_dir)
        monkeypatch.setattr(util_mod, "_resolve_compose_cmd", lambda: ["docker-compose"])

        calls: list[dict] = []

        class _FakeCompleted:
            def __init__(self):
                self.returncode = 0
                self.stdout = "Creating searxng_searxng_1 ... done"
                self.stderr = ""

        def fake_run(argv, cwd=None, capture_output=None, text=None, timeout=None):
            calls.append({"argv": argv, "cwd": cwd, "timeout": timeout})
            return _FakeCompleted()

        monkeypatch.setattr(util_mod.subprocess, "run", fake_run)

        printed: list[tuple] = []
        monkeypatch.setattr(
            egg_app,
            "console_print_block",
            lambda title, text, **kw: printed.append((title, text, kw.get("border_style"))),
        )

        egg_app.cmd_startSearxng("")

        import time
        deadline = time.time() + 2.0
        while time.time() < deadline and not calls:
            time.sleep(0.02)

        assert calls, "compose run was never invoked"
        assert calls[0]["argv"] == ["docker-compose", "up", "-d"]
        assert calls[0]["cwd"] == str(compose_dir)

        # Wait for the success block to appear.
        deadline = time.time() + 2.0
        while time.time() < deadline and not any("localhost:8888" in t[1] for t in printed):
            time.sleep(0.02)

        # Block has a "SearXNG start" title, green border, and embeds the captured stdout.
        assert any(t[0] == "SearXNG start" and t[2] == "green" for t in printed)
        assert any("Creating searxng_searxng_1" in t[1] for t in printed)
        # Immediate "starting…" line appears in the system log before the thread finishes.
        assert any("SearXNG start:" in m and "starting container" in m
                   for m in egg_app._system_log)


class TestCmdStopSearxng:
    """Tests for cmd_stopSearxng()."""

    def test_reports_missing_searxng_dir(self, egg_app, monkeypatch):
        import egg.commands.utility as util_mod
        monkeypatch.setattr(util_mod, "_find_searxng_dir", lambda: None)

        egg_app.cmd_stopSearxng("")

        assert any("could not locate searxng" in m for m in egg_app._system_log)

    def test_runs_compose_down(self, egg_app, monkeypatch, tmp_path):
        """Spawns a thread that runs `<compose> down` in the searxng dir."""
        import egg.commands.utility as util_mod

        compose_dir = tmp_path / "searxng"
        compose_dir.mkdir()
        (compose_dir / "docker-compose.yml").write_text("services: {}\n")

        monkeypatch.setattr(util_mod, "_find_searxng_dir", lambda: compose_dir)
        monkeypatch.setattr(util_mod, "_resolve_compose_cmd", lambda: ["docker-compose"])

        calls: list[dict] = []

        class _FakeCompleted:
            def __init__(self):
                self.returncode = 0
                self.stdout = "Stopping egg-searxng ... done\nRemoving egg-searxng ... done"
                self.stderr = ""

        def fake_run(argv, cwd=None, capture_output=None, text=None, timeout=None):
            calls.append({"argv": argv})
            return _FakeCompleted()

        monkeypatch.setattr(util_mod.subprocess, "run", fake_run)

        printed: list[tuple] = []
        monkeypatch.setattr(
            egg_app,
            "console_print_block",
            lambda title, text, **kw: printed.append((title, text, kw.get("border_style"))),
        )

        egg_app.cmd_stopSearxng("")

        import time
        deadline = time.time() + 2.0
        while time.time() < deadline and not calls:
            time.sleep(0.02)

        assert calls[0]["argv"] == ["docker-compose", "down"]

        deadline = time.time() + 2.0
        while time.time() < deadline and not any("Container stopped" in t[1] for t in printed):
            time.sleep(0.02)
        assert any(t[0] == "SearXNG stop" and t[2] == "green" for t in printed)
        assert any("Stopping egg-searxng" in t[1] for t in printed)

    def test_failure_surfaces_stderr_in_block(self, egg_app, monkeypatch, tmp_path):
        """Nonzero exit produces a red block with the captured output."""
        import egg.commands.utility as util_mod

        compose_dir = tmp_path / "searxng"
        compose_dir.mkdir()
        (compose_dir / "docker-compose.yml").write_text("services: {}\n")

        monkeypatch.setattr(util_mod, "_find_searxng_dir", lambda: compose_dir)
        monkeypatch.setattr(util_mod, "_resolve_compose_cmd", lambda: ["docker-compose"])

        class _FakeCompleted:
            returncode = 1
            stdout = ""
            stderr = "ERROR: No such network: egg_default"

        monkeypatch.setattr(util_mod.subprocess, "run",
                            lambda *a, **kw: _FakeCompleted())

        printed: list[tuple] = []
        monkeypatch.setattr(
            egg_app,
            "console_print_block",
            lambda title, text, **kw: printed.append((title, text, kw.get("border_style"))),
        )

        egg_app.cmd_stopSearxng("")

        import time
        deadline = time.time() + 2.0
        while time.time() < deadline and not printed:
            time.sleep(0.02)

        assert any(t[0] == "SearXNG stop" and t[2] == "red" for t in printed)
        assert any("No such network: egg_default" in t[1] for t in printed)
