"""Tests for commands/sandbox.py SandboxCommandsMixin."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure project root is in path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class TestCmdToggleSandboxing:
    """Tests for cmd_toggleSandboxing()."""

    def test_toggles_sandbox_state(self, egg_app, monkeypatch):
        """Should toggle sandbox enabled state."""
        toggled = []
        def mock_get_status(db, tid):
            return {'enabled': False, 'effective': True, 'provider': 'docker'}
        def mock_get_config(db, tid):
            class MockConfig:
                settings = {}
            return MockConfig()
        def mock_set_config(db, tid, enabled=None, settings=None, reason=None):
            toggled.append(enabled)
        monkeypatch.setattr("eggthreads.get_thread_sandbox_status", mock_get_status)
        monkeypatch.setattr("eggthreads.get_thread_sandbox_config", mock_get_config)
        monkeypatch.setattr("eggthreads.set_thread_sandbox_config", mock_set_config)
        monkeypatch.setattr("eggthreads.is_user_sandbox_control_enabled", lambda db, tid: True)

        egg_app.cmd_toggleSandboxing("")

        assert len(toggled) == 1
        assert toggled[0] is True  # Was False, now True

    def test_logs_enabled_message(self, egg_app, monkeypatch):
        """Should log enabled message when toggling on."""
        def mock_get_status(db, tid):
            return {'enabled': True, 'effective': True, 'provider': 'docker'}
        def mock_get_config(db, tid):
            class MockConfig:
                settings = {}
            return MockConfig()
        def mock_set_config(db, tid, **kwargs):
            pass
        monkeypatch.setattr("eggthreads.get_thread_sandbox_status", mock_get_status)
        monkeypatch.setattr("eggthreads.get_thread_sandbox_config", mock_get_config)
        monkeypatch.setattr("eggthreads.set_thread_sandbox_config", mock_set_config)
        monkeypatch.setattr("eggthreads.is_user_sandbox_control_enabled", lambda db, tid: True)

        egg_app.cmd_toggleSandboxing("")

        assert any("Sandboxing" in msg or "sandbox" in msg.lower() for msg in egg_app._system_log)

    def test_blocks_when_user_control_disabled(self, egg_app, monkeypatch):
        """Should block toggle when user sandbox control is disabled."""
        monkeypatch.setattr("eggthreads.is_user_sandbox_control_enabled", lambda db, tid: False)

        egg_app.cmd_toggleSandboxing("")

        assert any("disabled" in msg.lower() for msg in egg_app._system_log)

    def test_shows_warning_when_not_effective(self, egg_app, monkeypatch):
        """Should show warning when sandboxing enabled but not effective."""
        call_count = [0]
        def mock_get_status(db, tid):
            call_count[0] += 1
            if call_count[0] == 1:
                return {'enabled': False, 'effective': False, 'provider': 'docker'}
            return {'enabled': True, 'effective': False, 'warning': 'Docker not available'}
        def mock_get_config(db, tid):
            class MockConfig:
                settings = {}
            return MockConfig()
        def mock_set_config(db, tid, **kwargs):
            pass
        monkeypatch.setattr("eggthreads.get_thread_sandbox_status", mock_get_status)
        monkeypatch.setattr("eggthreads.get_thread_sandbox_config", mock_get_config)
        monkeypatch.setattr("eggthreads.set_thread_sandbox_config", mock_set_config)
        monkeypatch.setattr("eggthreads.is_user_sandbox_control_enabled", lambda db, tid: True)

        egg_app.cmd_toggleSandboxing("")

        assert any("not effective" in msg.lower() or "warning" in msg.lower() for msg in egg_app._system_log)

    def test_handles_error_gracefully(self, egg_app, monkeypatch):
        """Should handle errors gracefully."""
        def mock_get_status(db, tid):
            raise Exception("Test error")
        monkeypatch.setattr("eggthreads.get_thread_sandbox_status", mock_get_status)
        monkeypatch.setattr("eggthreads.is_user_sandbox_control_enabled", lambda db, tid: True)

        egg_app.cmd_toggleSandboxing("")

        assert any("error" in msg.lower() for msg in egg_app._system_log)


class TestCmdSetSandboxConfiguration:
    """Tests for cmd_setSandboxConfiguration()."""

    def test_shows_help_without_argument(self, egg_app, monkeypatch):
        """Should show help when no config name given."""
        printed = []
        monkeypatch.setattr(egg_app, "console_print_block", lambda *a, **k: printed.append(a))
        monkeypatch.setattr("eggthreads.is_user_sandbox_control_enabled", lambda db, tid: True)

        egg_app.cmd_setSandboxConfiguration("")

        assert len(printed) >= 1
        # Should print help block
        assert any("Sandbox" in str(p) for p in printed)

    def test_applies_config_with_name(self, egg_app, monkeypatch):
        """Should apply sandbox config by name."""
        applied = []
        def mock_set(db, tid, enabled=None, config_name=None, reason=None):
            applied.append((tid, config_name))
        monkeypatch.setattr("eggthreads.set_thread_sandbox_config", mock_set)
        monkeypatch.setattr("eggthreads.is_user_sandbox_control_enabled", lambda db, tid: True)

        egg_app.cmd_setSandboxConfiguration("my_config.json")

        assert len(applied) == 1
        assert applied[0][1] == "my_config.json"

    def test_logs_success_message(self, egg_app, monkeypatch):
        """Should log success message after applying config."""
        monkeypatch.setattr("eggthreads.set_thread_sandbox_config", lambda *a, **k: None)
        monkeypatch.setattr("eggthreads.is_user_sandbox_control_enabled", lambda db, tid: True)

        egg_app.cmd_setSandboxConfiguration("test.json")

        assert any("applied" in msg.lower() or "configuration" in msg.lower() for msg in egg_app._system_log)

    def test_blocks_when_user_control_disabled(self, egg_app, monkeypatch):
        """Should block when user sandbox control is disabled."""
        monkeypatch.setattr("eggthreads.is_user_sandbox_control_enabled", lambda db, tid: False)

        egg_app.cmd_setSandboxConfiguration("test.json")

        assert any("disabled" in msg.lower() for msg in egg_app._system_log)

    def test_handles_error_gracefully(self, egg_app, monkeypatch):
        """Should handle errors gracefully."""
        def mock_set(db, tid, **kwargs):
            raise Exception("Config not found")
        monkeypatch.setattr("eggthreads.set_thread_sandbox_config", mock_set)
        monkeypatch.setattr("eggthreads.is_user_sandbox_control_enabled", lambda db, tid: True)

        egg_app.cmd_setSandboxConfiguration("nonexistent.json")

        assert any("error" in msg.lower() for msg in egg_app._system_log)


class TestCmdGetSandboxingConfig:
    """Tests for cmd_getSandboxingConfig()."""

    def test_displays_current_config(self, egg_app, monkeypatch):
        """Should display current sandbox configuration."""
        printed = []
        def mock_get_status(db, tid):
            return {
                'provider': 'docker',
                'enabled': True,
                'available': True,
                'effective': True,
                'config_source': 'thread',
                'config_path': '.egg/sandbox/default.json'
            }
        monkeypatch.setattr("eggthreads.get_thread_sandbox_status", mock_get_status)
        monkeypatch.setattr(egg_app, "console_print_block", lambda *a, **k: printed.append(a))

        egg_app.cmd_getSandboxingConfig("")

        assert len(printed) >= 1
        # Should include config info
        assert any("Configuration" in str(p) or "configuration" in str(p).lower() for p in printed)

    def test_shows_provider_info(self, egg_app, monkeypatch):
        """Should show provider info."""
        printed = []
        def mock_get_status(db, tid):
            return {
                'provider': 'docker',
                'enabled': True,
                'available': True,
                'effective': True
            }
        monkeypatch.setattr("eggthreads.get_thread_sandbox_status", mock_get_status)
        monkeypatch.setattr(egg_app, "console_print_block", lambda title, text, **k: printed.append((title, text)))

        egg_app.cmd_getSandboxingConfig("")

        assert len(printed) >= 1
        # Check that provider is mentioned in the text
        assert "Provider" in printed[0][1] or "provider" in printed[0][1].lower()

    def test_shows_warning_if_present(self, egg_app, monkeypatch):
        """Should show warning if present."""
        printed = []
        def mock_get_status(db, tid):
            return {
                'provider': 'srt',
                'enabled': True,
                'available': False,
                'effective': False,
                'warning': 'SRT not installed'
            }
        monkeypatch.setattr("eggthreads.get_thread_sandbox_status", mock_get_status)
        monkeypatch.setattr(egg_app, "console_print_block", lambda title, text, **k: printed.append((title, text)))

        egg_app.cmd_getSandboxingConfig("")

        assert len(printed) >= 1
        # Should include warning
        assert "Warning" in printed[0][1] or "warning" in printed[0][1].lower() or "SRT" in printed[0][1]

    def test_logs_system_message(self, egg_app, monkeypatch):
        """Should log a system message."""
        def mock_get_status(db, tid):
            return {'provider': 'docker', 'enabled': True, 'available': True, 'effective': True}
        monkeypatch.setattr("eggthreads.get_thread_sandbox_status", mock_get_status)
        monkeypatch.setattr(egg_app, "console_print_block", lambda *a, **k: None)

        egg_app.cmd_getSandboxingConfig("")

        assert any("sandbox" in msg.lower() or "configuration" in msg.lower() for msg in egg_app._system_log)

    def test_handles_error_gracefully(self, egg_app, monkeypatch):
        """Should handle errors gracefully."""
        def mock_get_status(db, tid):
            raise Exception("Database error")
        monkeypatch.setattr("eggthreads.get_thread_sandbox_status", mock_get_status)

        egg_app.cmd_getSandboxingConfig("")

        assert any("error" in msg.lower() for msg in egg_app._system_log)
