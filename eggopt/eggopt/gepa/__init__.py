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
    EggthreadsCandidateProposer,
    ReflectionConversation,
    ReflectionDrive,
    ReflectionOccurrence,
)
from .runner import optimize_with_egg

__all__ = [
    "CandidateMutation",
    "EggflowGEPAAdapter",
    "EggthreadsCandidateProposer",
    "EvaluationSemanticKey",
    "ExampleEvaluation",
    "GEPAResult",
    "ReflectionConversation",
    "ReflectionDrive",
    "ReflectionEvidence",
    "ReflectionOccurrence",
    "optimize_with_egg",
]
