"""Public Egg integration for upstream GEPA."""

from gepa import GEPAResult

from .evaluation import (
    EggflowGEPAAdapter,
    EvaluationSemanticKey,
    ExampleEvaluation,
    ReflectionEvidence,
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
from .runner import optimize_with_egg

__all__ = [
    "CandidateMutation",
    "CandidateMutations",
    "EggflowGEPAAdapter",
    "EggthreadsCandidateProposer",
    "EggthreadsReflectionLM",
    "EvaluationSemanticKey",
    "ExampleEvaluation",
    "GEPAResult",
    "ReflectionConversation",
    "ReflectionDrive",
    "ReflectionEvidence",
    "ReflectionOccurrence",
    "optimize_with_egg",
]
