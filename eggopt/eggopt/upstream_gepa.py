from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Generic, TypeVar

from eggflow import FlowExecutor, TaskStore
from eggthreads import RunnerConfig, ThreadsDB, ToolRegistry

from .gepa.evaluation import EggflowGEPAAdapter, ExampleEvaluation
from .gepa.production_drive import EggthreadsReflectionDrive
from .gepa.reflection import EggthreadsReflectionLM
from .gepa.runner import optimize_with_egg
from .runtime import Reflection, Runtime

ExampleT = TypeVar("ExampleT")
OutputT = TypeVar("OutputT")


class UpstreamGEPA(Generic[ExampleT, OutputT]):
    """The external GEPA algorithm with Egg's durable runtime hidden inside."""

    def __init__(
        self,
        *,
        metric: Any | None = None,
        reflection: Reflection | None = None,
        reflection_lm: Any | None = None,
        reflection_tools: ToolRegistry | None = None,
        run_dir: str | Path | None = None,
        metric_identity: Any | None = None,
        example_id: Any | None = None,
        max_concurrent_evaluations: int | None = None,
        **options: Any,
    ) -> None:
        if reflection is None and reflection_lm is not None:
            if run_dir is None:
                raise TypeError("run_dir is required with reflection_lm")
            reflection = Reflection.eggthreads(
                llm=reflection_lm,
                tools=reflection_tools or ToolRegistry(),
                identity={"model": str(reflection_lm)},
                workspace=Path(run_dir) / "workspaces" / "mutation",
                runner_config=RunnerConfig(),
            )
        self.metric = metric
        self.reflection = reflection
        self.run_dir = Path(run_dir) if run_dir is not None else None
        self.metric_identity = metric_identity or _callable_identity(metric)
        self.example_id = example_id or (lambda example: example)
        self.max_concurrent_evaluations = max_concurrent_evaluations
        self.options = options

    def compile(
        self,
        student: Mapping[str, str] | str,
        *,
        trainset: Sequence[ExampleT],
        valset: Sequence[ExampleT] | None = None,
        adapter: EggflowGEPAAdapter[ExampleT, OutputT] | None = None,
        proposer: EggthreadsReflectionLM | None = None,
        flow: FlowExecutor | None = None,
        **options: Any,
    ) -> Any:
        seed = {"prompt": student} if isinstance(student, str) else dict(student)
        if proposer is not None:
            if adapter is None:
                if flow is None:
                    raise TypeError("flow is required when constructing an adapter")
                adapter = self._adapter(flow)
            proposer.resume_uncommitted()
            return self._optimize(seed, list(trainset), valset, adapter, proposer, **options)
        if self.reflection is None or self.run_dir is None:
            raise TypeError("reflection and run_dir are required for managed compile()")
        with Runtime.open(self.run_dir, self.reflection) as runtime:
            runtime.reflection.resume_uncommitted()
            return self._optimize(
                seed,
                list(trainset),
                valset,
                adapter or self._adapter(runtime.flow),
                runtime.reflection,
                **options,
            )

    def compile_with(
        self,
        student: Mapping[str, str] | str,
        *,
        trainset: Sequence[ExampleT],
        valset: Sequence[ExampleT] | None,
        adapter: EggflowGEPAAdapter[ExampleT, OutputT],
        proposer: EggthreadsReflectionLM,
        **options: Any,
    ) -> Any:
        """Advanced seam for a domain-owned adapter; lifecycle still stays here."""

        seed = {"prompt": student} if isinstance(student, str) else dict(student)
        proposer.resume_uncommitted()
        return self._optimize(seed, list(trainset), valset, adapter, proposer, **options)

    # ``adapter`` and ``proposer`` are an advanced compatibility seam for
    # domains with custom reflective projections. Normal clients never see it.

    def _adapter(self, flow: FlowExecutor) -> EggflowGEPAAdapter[ExampleT, OutputT]:
        if self.metric is None:
            raise TypeError("metric or a custom adapter is required")
        return EggflowGEPAAdapter(
            flow,
            evaluator=self.metric,
            evaluator_id="eggopt.metric",
            evaluator_version="1",
            evaluator_config=self.metric_identity,
            example_id=self.example_id,
            max_concurrent_evaluations=self.max_concurrent_evaluations,
        )

    def _optimize(self, seed, trainset, valset, adapter, proposer, **options):
        merged = {**self.options, **options}
        if self.run_dir is not None and "run_dir" not in merged:
            merged.setdefault("run_dir", str(self.run_dir / "upstream"))
        return optimize_with_egg(
            seed_candidate=seed,
            trainset=trainset,
            valset=list(valset) if valset is not None else None,
            adapter=adapter,
            proposer=proposer,
            **merged,
        )


def _callable_identity(function: Any) -> Mapping[str, str]:
    if function is None:
        return {}
    return {
        "module": getattr(function, "__module__", ""),
        "name": getattr(function, "__qualname__", function.__class__.__qualname__),
    }


__all__ = [
    "EggflowGEPAAdapter",
    "EggthreadsReflectionDrive",
    "EggthreadsReflectionLM",
    "ExampleEvaluation",
    "FlowExecutor",
    "TaskStore",
    "ThreadsDB",
    "UpstreamGEPA",
]
