"""Public Egg integration for upstream GEPA."""

from gepa import GEPAResult

from .evaluation import (
    EggflowGEPAAdapter,
    EvaluationSemanticKey,
    ExampleEvaluation,
    ReflectionEvidence,
    semantic_workspace_path,
)
from .reflection import (
    CandidateMutation,
    CandidateMutations,
    EggthreadsCandidateProposer,
    EggthreadsReflectionLM,
    ReflectionConversation,
    ReflectionDrive,
    ReflectionOccurrence,
)
from .production_drive import (
    EggthreadsReflectionDrive,
    SOLVER_SAFE_PROFILE_NAME,
    SOLVER_SAFE_PROFILE_VERSION,
    SOLVER_SAFE_TOOLS,
    configure_solver_safe_tools,
    create_solver_safe_study,
)
from .runner import optimize_with_egg

__all__ = [
    "CandidateMutation",
    "CandidateMutations",
    "EggflowGEPAAdapter",
    "EggthreadsCandidateProposer",
    "EggthreadsReflectionDrive",
    "EggthreadsReflectionLM",
    "SOLVER_SAFE_PROFILE_NAME",
    "SOLVER_SAFE_PROFILE_VERSION",
    "SOLVER_SAFE_TOOLS",
    "EvaluationSemanticKey",
    "ExampleEvaluation",
    "GEPAResult",
    "ReflectionConversation",
    "ReflectionDrive",
    "ReflectionEvidence",
    "ReflectionOccurrence",
    "semantic_workspace_path",
    "configure_solver_safe_tools",
    "create_solver_safe_study",
    "optimize_with_egg",
]
