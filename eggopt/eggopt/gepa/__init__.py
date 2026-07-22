"""Low-level GEPA integration pieces.

Most applications should use :class:`eggopt.UpstreamGEPA` or
:class:`eggopt.NativeGEPA`. These names remain for advanced integrations and
backward compatibility.
"""

from .evaluation import (
    EggflowGEPAAdapter,
    EvaluationSemanticKey,
    ExampleEvaluation,
    ReflectionEvidence,
    semantic_workspace_path,
)
from .production_drive import (
    SOLVER_SAFE_PROFILE_NAME,
    SOLVER_SAFE_PROFILE_VERSION,
    SOLVER_SAFE_TOOLS,
    EggthreadsReflectionDrive,
    configure_solver_safe_tools,
    create_solver_safe_study,
    default_solver_safe_tools,
    solver_safe_tools,
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

__all__ = [
    "SOLVER_SAFE_PROFILE_NAME",
    "SOLVER_SAFE_PROFILE_VERSION",
    "SOLVER_SAFE_TOOLS",
    "CandidateMutation",
    "CandidateMutations",
    "EggflowGEPAAdapter",
    "EggthreadsCandidateProposer",
    "EggthreadsReflectionDrive",
    "EggthreadsReflectionLM",
    "EvaluationSemanticKey",
    "ExampleEvaluation",
    "ReflectionConversation",
    "ReflectionDrive",
    "ReflectionEvidence",
    "ReflectionOccurrence",
    "configure_solver_safe_tools",
    "create_solver_safe_study",
    "default_solver_safe_tools",
    "solver_safe_tools",
    "semantic_workspace_path",
]


def __getattr__(name: str):
    if name in {"GEPAResult", "optimize_with_egg"}:
        from .runner import GEPAResult, optimize_with_egg

        return {"GEPAResult": GEPAResult, "optimize_with_egg": optimize_with_egg}[name]
    if name in {"MaxMutationStagesStopper", "ParetoBreadthSampling"}:
        from .search import MaxMutationStagesStopper, ParetoBreadthSampling

        return {
            "MaxMutationStagesStopper": MaxMutationStagesStopper,
            "ParetoBreadthSampling": ParetoBreadthSampling,
        }[name]
    raise AttributeError(name)
