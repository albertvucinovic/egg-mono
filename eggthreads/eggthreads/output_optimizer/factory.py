from __future__ import annotations

"""Factory helpers for composing native output optimizer filters."""

from .core import OutputFilter, OutputOptimizer
from .filters.cargo import CargoTestFailureSummaryFilter
from .filters.find import FindPathGroupFilter
from .filters.git_diff import GitDiffCompactFilter
from .filters.git_status import GitStatusCompactFilter
from .filters.grep import GrepRgGroupByFileFilter
from .filters.pytest import PytestFailureSummaryFilter
from .filters.python_traceback import PythonTracebackFocusFilter
from .filters.rtk import RtkPipeFilter
from .generic import GenericOutputFilter


def default_native_filters(
    *,
    bounded_max_chars: int | None = None,
    include_rtk: bool = False,
    rtk_command: str | None = None,
    rtk_timeout_seconds: float | None = None,
    rtk_privacy_opt_in: bool = False,
) -> list[OutputFilter]:
    """Return enabled native optimizer filters in conservative order."""

    filters: list[OutputFilter] = [
        GrepRgGroupByFileFilter(),
        FindPathGroupFilter(),
        GitStatusCompactFilter(),
        GitDiffCompactFilter(),
        PytestFailureSummaryFilter(),
        CargoTestFailureSummaryFilter(),
        PythonTracebackFocusFilter(),
    ]
    if include_rtk:
        kwargs = {"privacy_opt_in": bool(rtk_privacy_opt_in)}
        if rtk_command is not None:
            kwargs["command"] = rtk_command
        if rtk_timeout_seconds is not None:
            kwargs["timeout_seconds"] = rtk_timeout_seconds
        filters.append(RtkPipeFilter(**kwargs))
    filters.append(GenericOutputFilter(max_chars=bounded_max_chars))
    return filters


def create_default_output_optimizer(
    *,
    min_size_chars: int = 0,
    min_confidence: float = 0.5,
    bounded_max_chars: int | None = None,
    include_rtk: bool = False,
    rtk_command: str | None = None,
    rtk_timeout_seconds: float | None = None,
    rtk_privacy_opt_in: bool = False,
) -> OutputOptimizer:
    """Create the enabled native optimizer with semantic filters before generic fallback."""

    return OutputOptimizer(
        default_native_filters(
            bounded_max_chars=bounded_max_chars,
            include_rtk=include_rtk,
            rtk_command=rtk_command,
            rtk_timeout_seconds=rtk_timeout_seconds,
            rtk_privacy_opt_in=rtk_privacy_opt_in,
        ),
        min_size_chars=min_size_chars,
        min_confidence=min_confidence,
    )


__all__ = ["create_default_output_optimizer", "default_native_filters"]
