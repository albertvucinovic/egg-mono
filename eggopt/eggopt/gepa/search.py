"""Reusable search topologies for upstream GEPA."""

from __future__ import annotations

import random
from collections import Counter
from dataclasses import dataclass
from typing import Any

from gepa.gepa_utils import remove_dominated_programs
from gepa.strategies.proposal_sampling import ProposalTask


@dataclass
class MaxMutationStagesStopper:
    """Stop after a fixed number of GEPA loop iterations (mutation stages)."""

    max_stages: int

    def __post_init__(self) -> None:
        if isinstance(self.max_stages, bool) or not isinstance(self.max_stages, int):
            raise TypeError("max_stages must be an integer")
        if self.max_stages < 1:
            raise ValueError("max_stages must be positive")

    def __call__(self, state: Any) -> bool:
        return state.i >= self.max_stages - 1


class ParetoBreadthSampling:
    """Produce a fixed-width stage from distinct Pareto parents when possible.

    When the Pareto archive contains fewer parents than ``width``, selected
    parents repeat so the mutator can still propose multiple alternatives from
    the available lineage. Once the archive has enough breadth, selection is
    without replacement within a stage.
    """

    def __init__(self, width: int, *, rng: random.Random | None = None) -> None:
        if isinstance(width, bool) or not isinstance(width, int) or width < 1:
            raise ValueError("width must be a positive integer")
        self.width = width
        self.rng = rng or random.Random(0)

    def sample_tasks(self, state, candidate_selector, batch_sampler, trainset):  # type: ignore[no-untyped-def]
        del candidate_selector
        frequencies = self._pareto_parent_frequencies(state)
        selected = self._select_parents(frequencies)
        tasks = []
        for parent_idx in selected:
            minibatch_ids = batch_sampler.next_minibatch_ids(trainset, state)
            tasks.append(
                ProposalTask(
                    parent_idx,
                    state.program_candidates[parent_idx],
                    minibatch_ids,
                    trainset.fetch(minibatch_ids),
                )
            )
        return tasks

    def _pareto_parent_frequencies(self, state: Any) -> Counter[int]:
        frontier = remove_dominated_programs(
            state.get_pareto_front_mapping(),
            scores=state.per_program_tracked_scores,
        )
        frequencies: Counter[int] = Counter()
        for candidates in frontier.values():
            frequencies.update(candidates)
        if not frequencies:
            raise ValueError("GEPA Pareto frontier contains no selectable parent")
        return frequencies

    def _select_parents(self, frequencies: Counter[int]) -> list[int]:
        remaining = Counter(frequencies)
        selected: list[int] = []
        while remaining and len(selected) < self.width:
            parent = self.rng.choice(
                [
                    candidate
                    for candidate in sorted(remaining)
                    for _ in range(remaining[candidate])
                ]
            )
            selected.append(parent)
            del remaining[parent]
        while len(selected) < self.width:
            selected.append(self.rng.choice(selected))
        return selected
