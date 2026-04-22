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

    def cmd_displayMode(self, arg: str) -> None:
        """Handle /displayMode <full-screen|inline> - switch rendering mode.

        full-screen: alt-screen TUI with in-app scroll (PageUp/PageDown,
            mouse wheel) and streaming rendered into the "static" area
            above the live region.
        inline: HEAD-style rendering with the terminal's native
            scrollback, smallest possible screen updates, and native
            mouse selection/scroll. Streaming shows inside the Chat
            Messages panel body.
        """
        which = (arg or '').strip().lower().replace('_', '-')
        aliases = {
            'full-screen': False,
            'fullscreen': False,
            'full': False,
            'tui': False,
            'altscreen': False,
            'alt-screen': False,
            'inline': True,
            'classic': True,
            'head': True,
            'legacy': True,
        }
        if which not in aliases:
            cur = 'inline' if getattr(self, '_display_is_inline', False) else 'full-screen'
            self.log_system(f"Usage: /displayMode (full-screen|inline)   (current: {cur})")
            return
        want_inline = aliases[which]
        if bool(getattr(self, '_display_is_inline', False)) == want_inline:
            cur = 'inline' if want_inline else 'full-screen'
            self.log_system(f"Display mode already {cur}.")
            return
        self._display_is_inline = want_inline
        self._pending_mode_change = True
        new_mode = 'inline' if want_inline else 'full-screen'
        self.log_system(f"Display mode switching to {new_mode}…")

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
