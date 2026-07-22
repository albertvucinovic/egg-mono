"""Small, durable optimization interfaces built on Eggflow and Eggthreads."""

from .evaluation import Evaluation
from .gepa import (
    CandidateMutation,
    CandidateMutations,
    EggflowGEPAAdapter,
    EggthreadsCandidateProposer,
    EggthreadsReflectionDrive,
    EggthreadsReflectionLM,
    EvaluationSemanticKey,
    ExampleEvaluation,
    ReflectionConversation,
    ReflectionDrive,
    ReflectionEvidence,
    ReflectionOccurrence,
    configure_solver_safe_tools,
    create_solver_safe_study,
    semantic_workspace_path,
)
from .native_gepa import NativeGEPA, NativeGEPAResult
from .runtime import Reflection

__all__ = [
    "CandidateMutation",
    "CandidateMutations",
    "EggflowGEPAAdapter",
    "EggthreadsCandidateProposer",
    "EggthreadsReflectionDrive",
    "EggthreadsReflectionLM",
    "Evaluation",
    "EvaluationSemanticKey",
    "ExampleEvaluation",
    "NativeGEPA",
    "NativeGEPAResult",
    "Reflection",
    "ReflectionConversation",
    "ReflectionDrive",
    "ReflectionEvidence",
    "ReflectionOccurrence",
    "configure_solver_safe_tools",
    "create_solver_safe_study",
    "semantic_workspace_path",
]


def __getattr__(name: str):
    if name == "UpstreamGEPA":
        from .upstream_gepa import UpstreamGEPA

        return UpstreamGEPA
    if name in {"GEPAResult", "optimize_with_egg"}:
        from .gepa import GEPAResult, optimize_with_egg

        return {"GEPAResult": GEPAResult, "optimize_with_egg": optimize_with_egg}[name]
    raise AttributeError(name)
