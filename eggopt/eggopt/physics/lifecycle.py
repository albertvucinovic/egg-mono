from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TerminalOutcome:
    """Absorbing non-goal state reported by a Physics domain adapter."""

    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("terminal outcome reason must be a non-empty string")


def classify_terminal_state(
    state: Any,
    *,
    is_goal: Callable[[Any], bool],
    terminal_outcome: Callable[[Any], TerminalOutcome | None] | None,
) -> str | None:
    """Return the trusted stopping reason for one domain state, if absorbing."""

    if is_goal(state):
        return "won"
    if terminal_outcome is None:
        return None
    outcome = terminal_outcome(state)
    if outcome is not None and not isinstance(outcome, TerminalOutcome):
        raise TypeError("terminal_outcome must return TerminalOutcome or None")
    return outcome.reason if outcome is not None else None


def terminal_feedback(reason: str) -> str:
    if reason == "won":
        return "The trusted application detected the goal. The Physics run is complete."
    return (
        f"The trusted domain reported a terminal state ({reason}). "
        "No further real action is possible."
    )


__all__ = ["TerminalOutcome"]
