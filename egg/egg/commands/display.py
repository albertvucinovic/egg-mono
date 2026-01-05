"""Display-related command mixins for the egg application."""
from __future__ import annotations

from typing import Any, Dict


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
