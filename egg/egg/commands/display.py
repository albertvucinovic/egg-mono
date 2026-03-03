"""Display-related command mixins for the egg application."""
from __future__ import annotations

from typing import Any, Dict

from rich import box as rich_box


class DisplayCommandsMixin:
    """Mixin providing display management commands: /togglePanel, /redraw."""

    def cmd_togglePanel(self, arg: str) -> None:
        """Handle /togglePanel command - show/hide a panel."""
        which = (arg or '').strip().lower()
        valid = {'chat', 'children', 'system'}
        if not which or which not in valid:
            states = ", ".join(
                f"{k}={'on' if self._panel_visible.get(k, True) else 'off'}" for k in sorted(valid)
            )
            self.log_system(f"Usage: /togglePanel (chat|children|system)   (current: {states})")
            return
        cur = bool(self._panel_visible.get(which, True))
        self._panel_visible[which] = not cur
        self.log_system(f"Panel '{which}' is now {'shown' if self._panel_visible[which] else 'hidden'}. ")

    def cmd_redraw(self, arg: str) -> None:
        """Handle /redraw command - redraw the static transcript."""
        # Redraw the static transcript to reflect current terminal width.
        self.log_system('Redrawing transcript (see console).')
        try:
            self.redraw_static_view(reason='manual')
        except Exception as e:
            self.log_system(f'/redraw error: {e}')

    def cmd_toggleBorders(self, arg: str) -> None:
        """Handle /toggleBorders command - toggle borders on all panels except Message Input."""
        self._borders_visible = not getattr(self, '_borders_visible', True)

        # Update box style for all output panels (not input panel).
        # Use MINIMAL (no side borders, just top/bottom lines) when borders are hidden.
        if self._borders_visible:
            # Restore original box styles
            original = getattr(self, '_original_box_styles', {})
            self.chat_output.style.box = original.get('chat', rich_box.SQUARE)
            self.system_output.style.box = original.get('system', rich_box.SQUARE)
            self.children_output.style.box = original.get('children', rich_box.SQUARE)
            self.approval_panel.style.box = original.get('approval', rich_box.SQUARE)
        else:
            # Set minimal box style (no borders)
            self.chat_output.style.box = rich_box.MINIMAL
            self.system_output.style.box = rich_box.MINIMAL
            self.children_output.style.box = rich_box.MINIMAL
            self.approval_panel.style.box = rich_box.MINIMAL

        state = 'on' if self._borders_visible else 'off'
        self.log_system(f"Borders are now {state}.")

        # Redraw to reflect the change
        try:
            self.redraw_static_view(reason='borders toggled')
        except Exception:
            pass
