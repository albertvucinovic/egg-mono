from __future__ import annotations

"""Pure Egg-native tool-output optimizer core.

The optimizer is intentionally presentation-layer only: it accepts an already
captured tool output string and returns an immutable decision describing an
optional shorter preview.  It never mutates the raw output or runs commands.
"""

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Protocol, runtime_checkable


DEFAULT_OPTIMIZER_NAME = "egg_native_output_optimizer"


def _freeze_value(value: Any) -> Any:
    """Recursively freeze common container values for dataclass fields."""

    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_value(val) for key, val in value.items()})
    if isinstance(value, tuple):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, list):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze_value(item) for item in value)
    if isinstance(value, frozenset):
        return frozenset(_freeze_value(item) for item in value)
    return value


def _freeze_mapping(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not value:
        return MappingProxyType({})
    frozen = _freeze_value(dict(value))
    if isinstance(frozen, Mapping):
        return frozen
    return MappingProxyType({})


@dataclass(frozen=True)
class OptimizeRequest:
    """Immutable input for a pure output optimization attempt."""

    tool_name: str = ""
    tool_args: Mapping[str, Any] = field(default_factory=dict)
    output: str = ""
    finished_reason: str = ""
    thread_id: str = ""
    tool_call_id: str = ""
    origin: str = ""
    user_tool_call: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool_args", _freeze_mapping(self.tool_args))
        object.__setattr__(self, "metadata", _freeze_mapping(self.metadata))


@dataclass(frozen=True)
class OptimizeDecision:
    """Immutable result of an output optimization attempt."""

    optimized: bool
    output: str
    optimizer_name: str
    filter_name: str | None
    raw_chars: int
    optimized_chars: int
    savings_pct: float
    reason: str
    confidence: float
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _freeze_mapping(self.metadata))


@runtime_checkable
class OutputFilter(Protocol):
    """Small protocol implemented by pure output optimizer filters."""

    name: str

    def optimize(self, request: OptimizeRequest) -> OptimizeDecision | None:
        """Return a candidate decision for *request*, or ``None`` to abstain."""


def calculate_size_metadata(raw_output: str, optimized_output: str) -> dict[str, Any]:
    """Return deterministic char-count and savings metadata for two strings."""

    raw_chars = len(raw_output)
    optimized_chars = len(optimized_output)
    savings_chars = raw_chars - optimized_chars
    savings_pct = (savings_chars / raw_chars * 100.0) if raw_chars else 0.0
    return {
        "raw_chars": raw_chars,
        "optimized_chars": optimized_chars,
        "savings_chars": savings_chars,
        "savings_pct": savings_pct,
    }


def make_decision(
    request: OptimizeRequest,
    output: str,
    *,
    optimized: bool = True,
    optimizer_name: str = DEFAULT_OPTIMIZER_NAME,
    filter_name: str | None = None,
    reason: str = "",
    confidence: float = 1.0,
    metadata: Mapping[str, Any] | None = None,
) -> OptimizeDecision:
    """Build an :class:`OptimizeDecision` with consistent size metadata."""

    sizes = calculate_size_metadata(request.output, output)
    merged_metadata: dict[str, Any] = {**sizes}
    if metadata:
        merged_metadata.update(dict(metadata))
    return OptimizeDecision(
        optimized=bool(optimized),
        output=output,
        optimizer_name=optimizer_name,
        filter_name=filter_name,
        raw_chars=sizes["raw_chars"],
        optimized_chars=sizes["optimized_chars"],
        savings_pct=sizes["savings_pct"],
        reason=reason,
        confidence=float(confidence),
        metadata=merged_metadata,
    )


class OutputOptimizer:
    """Ordered registry/orchestrator for pure output optimizer filters.

    Filters are tried in registration order.  The first candidate that clears
    the size, confidence, and never-worse guards is returned.  Rejected filters
    and exceptions are recorded in the fallback metadata instead of bubbling to
    callers, preserving the invariant that optimizer failures leave output
    unchanged.
    """

    def __init__(
        self,
        filters: Iterable[OutputFilter] | None = None,
        *,
        name: str = DEFAULT_OPTIMIZER_NAME,
        min_size_chars: int = 0,
        min_confidence: float = 0.5,
    ) -> None:
        if min_size_chars < 0:
            raise ValueError("min_size_chars must be >= 0")
        if min_confidence < 0.0:
            raise ValueError("min_confidence must be >= 0")
        self.name = str(name or DEFAULT_OPTIMIZER_NAME)
        self.min_size_chars = int(min_size_chars)
        self.min_confidence = float(min_confidence)
        self._filters: list[OutputFilter] = []
        self._filter_names: set[str] = set()
        for output_filter in filters or ():
            self.register(output_filter)

    @property
    def filters(self) -> tuple[OutputFilter, ...]:
        """Registered filters in execution order."""

        return tuple(self._filters)

    def names(self) -> list[str]:
        """Registered filter names in execution order."""

        return [self._filter_name(output_filter) for output_filter in self._filters]

    def register(self, output_filter: OutputFilter) -> None:
        """Register *output_filter* at the end of the ordered filter list."""

        name = self._filter_name(output_filter)
        if not name:
            raise ValueError("Output optimizer filter name must not be empty")
        if name in self._filter_names:
            raise ValueError(f"Output optimizer filter already registered: {name}")
        self._filters.append(output_filter)
        self._filter_names.add(name)

    def optimize(self, request: OptimizeRequest) -> OptimizeDecision:
        """Return an accepted optimization decision or an unchanged fallback."""

        raw_chars = len(request.output)
        if raw_chars < self.min_size_chars:
            return self._fallback_decision(
                request,
                "below_min_size",
                {
                    "fallback": True,
                    "min_size_chars": self.min_size_chars,
                    "filter_count": len(self._filters),
                },
            )

        if not self._filters:
            return self._fallback_decision(
                request,
                "no_filters",
                {"fallback": True, "filter_count": 0},
            )

        rejections: list[Mapping[str, Any]] = []
        for output_filter in self._filters:
            filter_name = self._filter_name(output_filter)
            try:
                candidate = output_filter.optimize(request)
            except Exception as exc:
                rejections.append(
                    {
                        "filter_name": filter_name,
                        "reason": "exception",
                        "exception_type": type(exc).__name__,
                        "exception_message": str(exc),
                    }
                )
                continue

            if candidate is None:
                rejections.append({"filter_name": filter_name, "reason": "abstained"})
                continue

            rejection = self._rejection_reason(request, candidate)
            if rejection is not None:
                rejections.append({"filter_name": filter_name, **rejection})
                continue

            accepted_metadata: dict[str, Any] = dict(candidate.metadata)
            accepted_metadata.update(
                {
                    "fallback": False,
                    "accepted_filter": filter_name,
                    "filter_count": len(self._filters),
                }
            )
            if rejections:
                accepted_metadata["rejected_filters"] = tuple(rejections)
            return make_decision(
                request,
                candidate.output,
                optimized=True,
                optimizer_name=self.name,
                filter_name=candidate.filter_name or filter_name,
                reason=candidate.reason or "accepted",
                confidence=candidate.confidence,
                metadata=accepted_metadata,
            )

        return self._fallback_decision(
            request,
            "no_filter_accepted",
            {
                "fallback": True,
                "filter_count": len(self._filters),
                "rejected_filters": tuple(rejections),
            },
        )

    def _rejection_reason(self, request: OptimizeRequest, candidate: OptimizeDecision) -> dict[str, Any] | None:
        if not candidate.optimized:
            return {"reason": "not_optimized"}
        if candidate.confidence < self.min_confidence:
            return {
                "reason": "low_confidence",
                "confidence": candidate.confidence,
                "min_confidence": self.min_confidence,
            }
        raw_chars = len(request.output)
        candidate_chars = len(candidate.output)
        if candidate_chars >= raw_chars:
            return {
                "reason": "not_smaller",
                "raw_chars": raw_chars,
                "optimized_chars": candidate_chars,
            }
        return None

    def _fallback_decision(self, request: OptimizeRequest, reason: str, metadata: Mapping[str, Any]) -> OptimizeDecision:
        return make_decision(
            request,
            request.output,
            optimized=False,
            optimizer_name=self.name,
            filter_name=None,
            reason=reason,
            confidence=1.0,
            metadata=metadata,
        )

    @staticmethod
    def _filter_name(output_filter: OutputFilter) -> str:
        return str(getattr(output_filter, "name", "") or "").strip()


class OutputOptimizerRegistry(OutputOptimizer):
    """Compatibility name for the ordered optimizer registry/orchestrator."""


__all__ = [
    "DEFAULT_OPTIMIZER_NAME",
    "OptimizeDecision",
    "OptimizeRequest",
    "OutputFilter",
    "OutputOptimizer",
    "OutputOptimizerRegistry",
    "calculate_size_metadata",
    "make_decision",
]
