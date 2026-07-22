"""Small, durable optimization interfaces built on Eggflow and Eggthreads."""

from .actor_critic import ActorCritic, ActorCriticResult, Agent
from ._context import current_evaluation
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
from .native_gepa import (
    GenerateCandidate,
    NativeGEPA,
    NativeGEPAConfig,
    NativeGEPAResult,
    OptimizationPlan,
    SelectParents,
    optimize_anything,
    plan_optimization,
)
from .runtime import Reflection

__all__ = [
    "ActorCritic",
    "ActorCriticResult",
    "Agent",
    "CandidateMutation",
    "CandidateMutations",
    "EggflowGEPAAdapter",
    "EggthreadsCandidateProposer",
    "EggthreadsReflectionDrive",
    "EggthreadsReflectionLM",
    "Evaluation",
    "EvaluationSemanticKey",
    "ExampleEvaluation",
    "GenerateCandidate",
    "NativeGEPA",
    "NativeGEPAConfig",
    "NativeGEPAResult",
    "OptimizationPlan",
    "SelectParents",
    "Reflection",
    "ReflectionConversation",
    "ReflectionDrive",
    "ReflectionEvidence",
    "ReflectionOccurrence",
    "configure_solver_safe_tools",
    "current_evaluation",
    "create_solver_safe_study",
    "semantic_workspace_path",
    "optimize_anything",
    "plan_optimization",
]


def __getattr__(name: str):
    if name == "UpstreamGEPA":
        from .upstream_gepa import UpstreamGEPA

        return UpstreamGEPA
    if name in {"GEPAResult", "optimize_with_egg"}:
        from .gepa import GEPAResult, optimize_with_egg

        return {"GEPAResult": GEPAResult, "optimize_with_egg": optimize_with_egg}[name]
    raise AttributeError(name)
