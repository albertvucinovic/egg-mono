"""Tests for commands/display.py DisplayCommandsMixin."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure project root is in path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class TestCmdTogglePanel:
    """Tests for cmd_togglePanel()."""

    def test_toggles_chat_panel(self, egg_app):
        """Should toggle chat panel visibility."""
        initial = egg_app._panel_visible.get('chat', True)

        egg_app.cmd_togglePanel("chat")

        assert egg_app._panel_visible['chat'] != initial

    def test_toggles_children_panel(self, egg_app):
        """Should toggle children panel visibility."""
        initial = egg_app._panel_visible.get('children', True)

        egg_app.cmd_togglePanel("children")

        assert egg_app._panel_visible['children'] != initial

    def test_toggles_system_panel(self, egg_app):
        """Should toggle system panel visibility."""
        initial = egg_app._panel_visible.get('system', True)

        egg_app.cmd_togglePanel("system")

        assert egg_app._panel_visible['system'] != initial

    def test_shows_usage_for_invalid_panel(self, egg_app):
        """Should show usage for unknown panel name."""
        egg_app.cmd_togglePanel("invalid_panel")

        assert any("Usage" in msg or "usage" in msg.lower() or "chat|children|system" in msg
                   for msg in egg_app._system_log)

    def test_logs_visibility_change(self, egg_app):
        """Should log the visibility change."""
        egg_app.cmd_togglePanel("chat")

        assert any("panel" in msg.lower() or "visible" in msg.lower() or "hidden" in msg.lower()
                   for msg in egg_app._system_log)


class TestCmdRedraw:
    """Tests for cmd_redraw()."""

    def test_calls_redraw_static_view(self, egg_app, monkeypatch):
        """Should call redraw_static_view(reason='manual')."""
        redrawn = []
        def mock_redraw(reason=None):
            redrawn.append(reason)
        monkeypatch.setattr(egg_app, "redraw_static_view", mock_redraw)

        egg_app.cmd_redraw("")

        assert len(redrawn) == 1
        assert redrawn[0] == "manual"

    def test_logs_redraw_action(self, egg_app, monkeypatch):
        """Should log the redraw action."""
        monkeypatch.setattr(egg_app, "redraw_static_view", lambda reason=None: None)

        egg_app.cmd_redraw("")

        assert any("redraw" in msg.lower() for msg in egg_app._system_log)
