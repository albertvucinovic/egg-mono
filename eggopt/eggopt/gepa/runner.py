from __future__ import annotations

from typing import Any

from gepa import GEPAResult, optimize

from .evaluation import EggflowGEPAAdapter
from .reflection import EggthreadsReflectionLM


def optimize_with_egg(
    *,
    seed_candidate: dict[str, str],
    trainset: list[Any],
    adapter: EggflowGEPAAdapter[Any, Any],
    proposer: EggthreadsReflectionLM,
    valset: list[Any] | None = None,
    **gepa_options: Any,
) -> GEPAResult:
    """Run upstream GEPA through Egg's evaluator and reflection boundaries.

    All archive, Pareto-frontier, lineage, selection, acceptance, budget, and
    result behavior remains upstream GEPA behavior. ``gepa_options`` is passed
    directly to :func:`gepa.optimize`.
    """

    forbidden = {
        "custom_candidate_proposer",
        "reflection_lm",
        "reflection_strategy",
        "task_lm",
        "evaluator",
    }
    overlap = forbidden.intersection(gepa_options)
    if overlap:
        raise TypeError(f"Egg integration owns GEPA option(s): {sorted(overlap)}")
    return optimize(
        seed_candidate=seed_candidate,
        trainset=trainset,
        valset=valset,
        adapter=adapter,
        reflection_strategy=proposer,
        **gepa_options,
    )
