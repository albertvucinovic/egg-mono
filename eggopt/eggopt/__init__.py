"""Egg-integrated optimization algorithms."""

from .gepa import (
    CandidateMutation,
    EggflowGEPAAdapter,
    EggthreadsCandidateProposer,
    EvaluationSemanticKey,
    ExampleEvaluation,
    GEPAResult,
    ReflectionConversation,
    ReflectionDrive,
    ReflectionEvidence,
    ReflectionOccurrence,
    optimize_with_egg,
)

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
