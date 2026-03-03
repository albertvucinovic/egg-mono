"""Tests for commands/display.py DisplayCommandsMixin."""
from __future__ import annotations

import pytest


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


class TestCmdToggleBorders:
    """Tests for cmd_toggleBorders()."""

    def test_toggles_borders_visibility(self, egg_app, monkeypatch):
        """Should toggle _borders_visible flag."""
        # Mock redraw to avoid side effects
        monkeypatch.setattr(egg_app, "redraw_static_view", lambda reason=None: None)

        initial = egg_app._borders_visible

        egg_app.cmd_toggleBorders("")

        assert egg_app._borders_visible != initial

    def test_toggles_back_to_original(self, egg_app, monkeypatch):
        """Should toggle back to original state on second call."""
        monkeypatch.setattr(egg_app, "redraw_static_view", lambda reason=None: None)

        initial = egg_app._borders_visible

        egg_app.cmd_toggleBorders("")
        egg_app.cmd_toggleBorders("")

        assert egg_app._borders_visible == initial

    def test_changes_output_panel_box_styles(self, egg_app, monkeypatch):
        """Should change box styles on output panels when toggling on."""
        from rich import box as rich_box

        monkeypatch.setattr(egg_app, "redraw_static_view", lambda reason=None: None)

        # Initially borders should be hidden (off by default)
        assert egg_app._borders_visible is False
        assert egg_app.chat_output.style.box == rich_box.MINIMAL

        egg_app.cmd_toggleBorders("")

        # After toggle, borders should be visible
        assert egg_app._borders_visible is True
        # Output panels should use original box style (SQUARE)
        assert egg_app.chat_output.style.box == egg_app._original_box_styles['chat']
        assert egg_app.system_output.style.box == egg_app._original_box_styles['system']
        assert egg_app.children_output.style.box == egg_app._original_box_styles['children']
        assert egg_app.approval_panel.style.box == egg_app._original_box_styles['approval']

    def test_restores_original_box_styles(self, egg_app, monkeypatch):
        """Should restore original box styles when toggling to on."""
        from rich import box as rich_box

        monkeypatch.setattr(egg_app, "redraw_static_view", lambda reason=None: None)

        # Store original styles (SQUARE, stored before being changed to MINIMAL)
        original_chat = egg_app._original_box_styles['chat']
        original_system = egg_app._original_box_styles['system']

        # Initially off (MINIMAL)
        assert egg_app.chat_output.style.box == rich_box.MINIMAL

        # Toggle to on - should restore original SQUARE styles
        egg_app.cmd_toggleBorders("")

        assert egg_app.chat_output.style.box == original_chat
        assert egg_app.system_output.style.box == original_system

    def test_does_not_affect_input_panel(self, egg_app, monkeypatch):
        """Should not change box style of input panel."""
        monkeypatch.setattr(egg_app, "redraw_static_view", lambda reason=None: None)

        original_input_box = egg_app.input_panel.style.box

        egg_app.cmd_toggleBorders("")

        # Input panel should be unchanged
        assert egg_app.input_panel.style.box == original_input_box

    def test_logs_state_change(self, egg_app, monkeypatch):
        """Should log the borders state change."""
        monkeypatch.setattr(egg_app, "redraw_static_view", lambda reason=None: None)

        egg_app.cmd_toggleBorders("")

        # Since borders are off by default, first toggle turns them on
        assert any("borders" in msg.lower() and "on" in msg.lower()
                   for msg in egg_app._system_log)

    def test_triggers_redraw(self, egg_app, monkeypatch):
        """Should trigger a redraw after toggling."""
        redrawn = []
        def mock_redraw(reason=None):
            redrawn.append(reason)
        monkeypatch.setattr(egg_app, "redraw_static_view", mock_redraw)

        egg_app.cmd_toggleBorders("")

        assert len(redrawn) == 1
        assert "borders" in redrawn[0].lower()
