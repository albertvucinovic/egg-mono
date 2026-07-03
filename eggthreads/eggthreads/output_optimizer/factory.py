from __future__ import annotations

"""Factory helpers for composing native output optimizer filters."""

from .core import OutputFilter, OutputOptimizer
from .filters.find import FindPathGroupFilter
from .filters.git_status import GitStatusCompactFilter
from .filters.grep import GrepRgGroupByFileFilter
from .filters.python_traceback import PythonTracebackFocusFilter
from .generic import GenericOutputFilter


def default_native_filters(*, bounded_max_chars: int | None = None) -> list[OutputFilter]:
    """Return enabled native optimizer filters in conservative order."""

    return [
        GrepRgGroupByFileFilter(),
        FindPathGroupFilter(),
        GitStatusCompactFilter(),
        PythonTracebackFocusFilter(),
        GenericOutputFilter(max_chars=bounded_max_chars),
    ]


def create_default_output_optimizer(
    *,
    min_size_chars: int = 0,
    min_confidence: float = 0.5,
    bounded_max_chars: int | None = None,
) -> OutputOptimizer:
    """Create the enabled native optimizer with semantic filters before generic fallback."""

    return OutputOptimizer(
        default_native_filters(bounded_max_chars=bounded_max_chars),
        min_size_chars=min_size_chars,
        min_confidence=min_confidence,
    )


__all__ = ["create_default_output_optimizer", "default_native_filters"]
