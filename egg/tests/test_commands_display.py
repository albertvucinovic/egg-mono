"""Tests for commands/display.py DisplayCommandsMixin."""
from __future__ import annotations

import pytest


class TestCmdTogglePanel:
    """Tests for /togglePanel."""

    def test_toggles_chat_panel(self, egg_app):
        """Should toggle chat panel visibility."""
        initial = egg_app._panel_visible.get('chat', True)

        egg_app.handle_command("/togglePanel " + "chat")

        assert egg_app._panel_visible['chat'] != initial

    def test_toggles_children_panel(self, egg_app):
        """Should toggle children panel visibility."""
        initial = egg_app._panel_visible.get('children', True)

        egg_app.handle_command("/togglePanel " + "children")

        assert egg_app._panel_visible['children'] != initial

    def test_toggles_system_panel(self, egg_app):
        """Should toggle system panel visibility."""
        initial = egg_app._panel_visible.get('system', True)

        egg_app.handle_command("/togglePanel " + "system")

        assert egg_app._panel_visible['system'] != initial

    def test_shows_usage_for_invalid_panel(self, egg_app):
        """Should show usage for unknown panel name."""
        egg_app.handle_command("/togglePanel " + "invalid_panel")

        assert any("Usage" in msg or "usage" in msg.lower() or "chat|children|system" in msg
                   for msg in egg_app._system_log)

    def test_logs_visibility_change(self, egg_app):
        """Should log the visibility change."""
        egg_app.handle_command("/togglePanel " + "chat")

        assert any("panel" in msg.lower() or "visible" in msg.lower() or "hidden" in msg.lower()
                   for msg in egg_app._system_log)


class TestCmdRedraw:
    """Tests for /redraw."""

    def test_calls_redraw_static_view(self, egg_app, monkeypatch):
        """Should call redraw_static_view(reason='manual')."""
        redrawn = []
        def mock_redraw(reason=None):
            redrawn.append(reason)
        monkeypatch.setattr(egg_app, "redraw_static_view", mock_redraw)

        egg_app.handle_command("/redraw")

        assert len(redrawn) == 1
        assert redrawn[0] == "manual"

    def test_logs_redraw_action(self, egg_app, monkeypatch):
        """Should log the redraw action."""
        monkeypatch.setattr(egg_app, "redraw_static_view", lambda reason=None: None)

        egg_app.handle_command("/redraw")

        assert any("redraw" in msg.lower() for msg in egg_app._system_log)


class TestCmdToggleBorders:
    """Tests for /toggleBorders."""

    def test_toggles_borders_visibility(self, egg_app, monkeypatch):
        """Should toggle _borders_visible flag."""
        # Mock redraw to avoid side effects
        monkeypatch.setattr(egg_app, "redraw_static_view", lambda reason=None: None)

        initial = egg_app._borders_visible

        egg_app.handle_command("/toggleBorders")

        assert egg_app._borders_visible != initial

    def test_toggles_back_to_original(self, egg_app, monkeypatch):
        """Should toggle back to original state on second call."""
        monkeypatch.setattr(egg_app, "redraw_static_view", lambda reason=None: None)

        initial = egg_app._borders_visible

        egg_app.handle_command("/toggleBorders")
        egg_app.handle_command("/toggleBorders")

        assert egg_app._borders_visible == initial

    def test_changes_output_panel_box_styles(self, egg_app, monkeypatch):
        """Should change box styles on output panels when toggling on."""
        from rich import box as rich_box

        monkeypatch.setattr(egg_app, "redraw_static_view", lambda reason=None: None)

        # Initially borders should be hidden (off by default)
        assert egg_app._borders_visible is False
        assert egg_app.chat_output.style.box == rich_box.MINIMAL

        egg_app.handle_command("/toggleBorders")

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
        egg_app.handle_command("/toggleBorders")

        assert egg_app.chat_output.style.box == original_chat
        assert egg_app.system_output.style.box == original_system

    def test_does_not_affect_input_panel(self, egg_app, monkeypatch):
        """Should not change box style of input panel."""
        monkeypatch.setattr(egg_app, "redraw_static_view", lambda reason=None: None)

        original_input_box = egg_app.input_panel.style.box

        egg_app.handle_command("/toggleBorders")

        # Input panel should be unchanged
        assert egg_app.input_panel.style.box == original_input_box

    def test_logs_state_change(self, egg_app, monkeypatch):
        """Should log the borders state change."""
        monkeypatch.setattr(egg_app, "redraw_static_view", lambda reason=None: None)

        egg_app.handle_command("/toggleBorders")

        # Since borders are off by default, first toggle turns them on
        assert any("borders" in msg.lower() and "on" in msg.lower()
                   for msg in egg_app._system_log)

    def test_triggers_redraw(self, egg_app, monkeypatch):
        """Should trigger a redraw after toggling."""
        redrawn = []
        def mock_redraw(reason=None):
            redrawn.append(reason)
        monkeypatch.setattr(egg_app, "redraw_static_view", mock_redraw)

        egg_app.handle_command("/toggleBorders")

        assert len(redrawn) == 1
        assert "borders" in redrawn[0].lower()


class TestCmdDisplayVerbosity:
    """Tests for /displayVerbosity."""

    def test_default_display_verbosity_is_max(self, egg_app):
        assert egg_app._display_verbosity == "max"

    def test_no_argument_reports_current_level_and_usage(self, egg_app):
        egg_app.handle_command("/displayVerbosity")

        assert egg_app._display_verbosity == "max"
        assert any(
            "/displayVerbosity <max|medium|min>" in msg and "current: max" in msg
            for msg in egg_app._system_log
        )

    def test_sets_display_verbosity(self, egg_app):
        egg_app.handle_command("/displayVerbosity medium")

        assert egg_app._display_verbosity == "medium"
        assert any("Display verbosity set to medium." in msg for msg in egg_app._system_log)

    def test_sets_display_verbosity_triggers_coherent_source_redraw(self, egg_app, monkeypatch):
        redrawn = []

        def record_redraw(**kwargs):
            redrawn.append(kwargs)

        monkeypatch.setattr(egg_app, "redraw_static_view", record_redraw)

        egg_app.handle_command("/displayVerbosity medium")

        assert redrawn == [{
            "reason": "display verbosity changed",
            "reuse_transcript_source": True,
        }]

    def test_same_display_verbosity_is_truthful_noop_without_redraw(self, egg_app, monkeypatch):
        redrawn = []
        monkeypatch.setattr(egg_app, "redraw_static_view", lambda **kwargs: redrawn.append(kwargs))

        egg_app.handle_command("/displayVerbosity max")

        assert egg_app._display_verbosity == "max"
        assert redrawn == []
        assert any("Display verbosity already max." in msg for msg in egg_app._system_log)

    def test_invalid_display_verbosity_reports_usage_without_changing_state(self, egg_app):
        egg_app.handle_command("/displayVerbosity min")
        egg_app.handle_command("/displayVerbosity compact")

        assert egg_app._display_verbosity == "min"
        assert any(
            "/displayVerbosity <max|medium|min>" in msg and "current: min" in msg
            for msg in egg_app._system_log
        )


class TestCmdTheme:
    """Tests for terminal Egg /theme."""

    def test_theme_command_is_egg_app_local(self, egg_app):
        from eggthreads.command_catalog import create_default_command_registry

        assert "theme" not in create_default_command_registry().names()
        assert "theme" in egg_app.command_registry.names()

    def test_no_argument_lists_themes(self, egg_app):
        egg_app.handle_command("/theme")

        assert egg_app._theme == "default"
        assert any("Available themes:" in msg and "current: default" in msg for msg in egg_app._system_log)
        assert any("mono" in msg and "light-mono" in msg for msg in egg_app._system_log)

    def test_sets_theme_and_redraws(self, egg_app, monkeypatch):
        redrawn = []
        monkeypatch.setattr(egg_app, "redraw_static_view", lambda reason=None: redrawn.append(reason))

        egg_app.handle_command("/theme matrix")

        assert egg_app._theme == "matrix"
        assert egg_app._rich_theme is not None
        assert any("Theme changed to: matrix" in msg for msg in egg_app._system_log)
        assert redrawn == ["theme changed"]

    def test_background_variant_aliases_to_terminal_palette(self, egg_app, monkeypatch):
        monkeypatch.setattr(egg_app, "redraw_static_view", lambda reason=None: None)

        egg_app.handle_command("/theme dark-background")

        assert egg_app._theme == "dark"

    def test_black_and_white_themes_from_eggw_are_supported(self, egg_app, monkeypatch):
        monkeypatch.setattr(egg_app, "redraw_static_view", lambda reason=None: None)

        egg_app.handle_command("/theme mono")
        assert egg_app._theme == "mono"

        egg_app.handle_command("/theme light-mono")
        assert egg_app._theme == "light-mono"

    def test_large_content_theme_styles_are_not_underlined(self):
        from egg.theme import THEMES, rich_theme_for

        large_content_styles = [
            "green",
            "cyan",
            "blue",
            "yellow",
            "magenta",
            "egg.user",
            "egg.assistant",
            "egg.system",
            "egg.tool",
            "egg.reasoning",
            "egg.tool_call",
            "egg.tool_call_dim",
        ]

        for name in THEMES:
            styles = rich_theme_for(name).styles
            for style_name in large_content_styles:
                assert not styles[style_name].underline, (name, style_name)

    def test_invalid_theme_reports_error_without_changing_state(self, egg_app, monkeypatch):
        monkeypatch.setattr(egg_app, "redraw_static_view", lambda reason=None: None)
        egg_app.handle_command("/theme matrix")

        egg_app.handle_command("/theme no-such-theme")

        assert egg_app._theme == "matrix"
        assert any("Unknown theme: no-such-theme" in msg for msg in egg_app._system_log)
