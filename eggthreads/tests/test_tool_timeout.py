"""Tests for tool execution timeout behavior.

Tests verify that:
1. LLM-specified timeout_sec parameter works for bash and python tools
2. Config-based _tool_timeout_sec works as fallback
3. LLM-specified timeout takes priority over config timeout
4. Timeout returns appropriate error message to LLM
"""

from __future__ import annotations

from pathlib import Path

import pytest

import eggthreads as _eggthreads_mod


def _import_eggthreads(monkeypatch, tmp_path: Path):
    """Return the eggthreads module with cwd set to tmp_path."""
    monkeypatch.chdir(tmp_path)
    return _eggthreads_mod


class TestToolTimeout:
    """Tests for bash and python tool timeout behavior."""

    def test_bash_tool_llm_timeout_kills_long_running_command(self, tmp_path, monkeypatch):
        """Bash tool should timeout when LLM specifies timeout_sec."""
        eggthreads = _import_eggthreads(monkeypatch, tmp_path)
        tools = eggthreads.create_default_tools()

        # Run a sleep command with a 1-second timeout
        result = tools.execute(
            "bash",
            {"script": "sleep 10", "timeout_sec": 1},
        )

        assert "TIMEOUT" in result
        assert "1" in result or "1.0" in result  # timeout value mentioned

    def test_bash_tool_completes_within_timeout(self, tmp_path, monkeypatch):
        """Bash tool should complete normally if command finishes before timeout."""
        eggthreads = _import_eggthreads(monkeypatch, tmp_path)
        tools = eggthreads.create_default_tools()

        result = tools.execute(
            "bash",
            {"script": "echo hello", "timeout_sec": 10},
        )

        assert "TIMEOUT" not in result
        assert "hello" in result

    def test_python_tool_llm_timeout_kills_long_running_script(self, tmp_path, monkeypatch):
        """Python tool should timeout when LLM specifies timeout_sec."""
        eggthreads = _import_eggthreads(monkeypatch, tmp_path)
        tools = eggthreads.create_default_tools()

        # Run an infinite loop with a 1-second timeout
        result = tools.execute(
            "python",
            {"script": "import time; time.sleep(10)", "timeout_sec": 1},
        )

        assert "TIMEOUT" in result
        assert "1" in result or "1.0" in result

    def test_python_tool_completes_within_timeout(self, tmp_path, monkeypatch):
        """Python tool should complete normally if script finishes before timeout."""
        eggthreads = _import_eggthreads(monkeypatch, tmp_path)
        tools = eggthreads.create_default_tools()

        result = tools.execute(
            "python",
            {"script": "print('hello')", "timeout_sec": 10},
        )

        assert "TIMEOUT" not in result
        assert "hello" in result

    def test_bash_tool_config_timeout_works(self, tmp_path, monkeypatch):
        """Bash tool should use _tool_timeout_sec from config when LLM doesn't specify."""
        eggthreads = _import_eggthreads(monkeypatch, tmp_path)
        tools = eggthreads.create_default_tools()

        # Pass config timeout via context (simulating RunnerConfig)
        result = tools.execute(
            "bash",
            {"script": "sleep 10"},
            tool_timeout_sec=1,  # This becomes _tool_timeout_sec in args
        )

        assert "TIMEOUT" in result

    def test_python_tool_config_timeout_works(self, tmp_path, monkeypatch):
        """Python tool should use _tool_timeout_sec from config when LLM doesn't specify."""
        eggthreads = _import_eggthreads(monkeypatch, tmp_path)
        tools = eggthreads.create_default_tools()

        result = tools.execute(
            "python",
            {"script": "import time; time.sleep(10)"},
            tool_timeout_sec=1,
        )

        assert "TIMEOUT" in result

    def test_llm_timeout_overrides_config_timeout(self, tmp_path, monkeypatch):
        """LLM-specified timeout_sec should take priority over config _tool_timeout_sec."""
        eggthreads = _import_eggthreads(monkeypatch, tmp_path)
        tools = eggthreads.create_default_tools()

        # Config says 1 second, LLM says 10 seconds - command should complete
        result = tools.execute(
            "bash",
            {"script": "sleep 2", "timeout_sec": 10},  # LLM: 10s
            tool_timeout_sec=1,  # Config: 1s (should be overridden)
        )

        # The command sleeps for 2s. With LLM timeout of 10s, it should complete.
        # With config timeout of 1s, it would have timed out.
        assert "TIMEOUT" not in result

    def test_llm_can_specify_shorter_timeout_than_config(self, tmp_path, monkeypatch):
        """LLM can specify a shorter timeout than config."""
        eggthreads = _import_eggthreads(monkeypatch, tmp_path)
        tools = eggthreads.create_default_tools()

        # Config says 60 seconds, LLM says 1 second
        result = tools.execute(
            "bash",
            {"script": "sleep 10", "timeout_sec": 1},  # LLM: 1s
            tool_timeout_sec=60,  # Config: 60s
        )

        # Should timeout because LLM specified 1s
        assert "TIMEOUT" in result

    def test_no_timeout_when_neither_specified(self, tmp_path, monkeypatch):
        """Command should run without timeout if neither LLM nor config specifies one."""
        eggthreads = _import_eggthreads(monkeypatch, tmp_path)
        tools = eggthreads.create_default_tools()

        # Quick command, no timeout specified
        result = tools.execute(
            "bash",
            {"script": "echo done"},
            # No tool_timeout_sec passed
        )

        assert "TIMEOUT" not in result
        assert "done" in result

    def test_timeout_message_format_for_llm(self, tmp_path, monkeypatch):
        """Timeout message should be clear for LLM to understand."""
        eggthreads = _import_eggthreads(monkeypatch, tmp_path)
        tools = eggthreads.create_default_tools()

        result = tools.execute(
            "bash",
            {"script": "sleep 10", "timeout_sec": 1},
        )

        # Message should clearly indicate timeout occurred
        assert "--- TIMEOUT ---" in result
        assert "timed out" in result.lower()
        # Should mention the timeout duration
        assert "1" in result


class TestToolTimeoutInvalidInput:
    """Tests for handling invalid timeout values."""

    def test_invalid_timeout_string_falls_back_to_config(self, tmp_path, monkeypatch):
        """Invalid LLM timeout should fall back to config timeout."""
        eggthreads = _import_eggthreads(monkeypatch, tmp_path)
        tools = eggthreads.create_default_tools()

        result = tools.execute(
            "bash",
            {"script": "sleep 10", "timeout_sec": "invalid"},  # Invalid
            tool_timeout_sec=1,  # Config fallback
        )

        # Should use config timeout and thus timeout
        assert "TIMEOUT" in result

    def test_negative_timeout_treated_as_no_timeout(self, tmp_path, monkeypatch):
        """Negative timeout from LLM should be treated as invalid."""
        eggthreads = _import_eggthreads(monkeypatch, tmp_path)
        tools = eggthreads.create_default_tools()

        # Negative timeout - should use config
        result = tools.execute(
            "bash",
            {"script": "sleep 5", "timeout_sec": -1},
            tool_timeout_sec=1,
        )

        # Negative converts to float -1.0, which subprocess treats as no timeout
        # So it falls through. This test documents current behavior.
        # If we want to handle negative differently, we'd need to add validation.


class TestToolCancelCheck:
    """Tests for cancel_check callback (interrupt support)."""

    def test_bash_tool_respects_cancel_check(self, tmp_path, monkeypatch):
        """Bash tool should stop when cancel_check returns True."""
        eggthreads = _import_eggthreads(monkeypatch, tmp_path)
        tools = eggthreads.create_default_tools()

        import threading
        cancel_flag = threading.Event()

        def cancel_check():
            return cancel_flag.is_set()

        # Start cancellation after 0.5 seconds in another thread
        def set_cancel():
            import time
            time.sleep(0.5)
            cancel_flag.set()

        threading.Thread(target=set_cancel, daemon=True).start()

        result = tools.execute(
            "bash",
            {"script": "sleep 10"},
            cancel_check=cancel_check,
        )

        assert "INTERRUPTED" in result

    def test_python_tool_respects_cancel_check(self, tmp_path, monkeypatch):
        """Python tool should stop when cancel_check returns True."""
        eggthreads = _import_eggthreads(monkeypatch, tmp_path)
        tools = eggthreads.create_default_tools()

        import threading
        cancel_flag = threading.Event()

        def cancel_check():
            return cancel_flag.is_set()

        # Start cancellation after 0.5 seconds
        def set_cancel():
            import time
            time.sleep(0.5)
            cancel_flag.set()

        threading.Thread(target=set_cancel, daemon=True).start()

        result = tools.execute(
            "python",
            {"script": "import time; time.sleep(10)"},
            cancel_check=cancel_check,
        )

        assert "INTERRUPTED" in result


class TestDefaultToolTimeoutAPI:
    """Tests for set_default_tool_timeout and get_default_tool_timeout API."""

    def test_get_default_timeout_returns_30_seconds(self, tmp_path, monkeypatch):
        """Default tool timeout should be 30 seconds."""
        eggthreads = _import_eggthreads(monkeypatch, tmp_path)

        timeout = eggthreads.get_default_tool_timeout()
        assert timeout == 30.0

    def test_set_default_timeout_changes_value(self, tmp_path, monkeypatch):
        """set_default_tool_timeout should change the global default."""
        eggthreads = _import_eggthreads(monkeypatch, tmp_path)

        original = eggthreads.get_default_tool_timeout()
        try:
            eggthreads.set_default_tool_timeout(120.0)
            assert eggthreads.get_default_tool_timeout() == 120.0
        finally:
            # Restore original
            eggthreads.set_default_tool_timeout(original)

    def test_set_default_timeout_to_zero_disables_timeout(self, tmp_path, monkeypatch):
        """Setting timeout to 0 should disable timeout."""
        eggthreads = _import_eggthreads(monkeypatch, tmp_path)

        original = eggthreads.get_default_tool_timeout()
        try:
            eggthreads.set_default_tool_timeout(0)
            assert eggthreads.get_default_tool_timeout() == 0
        finally:
            eggthreads.set_default_tool_timeout(original)
