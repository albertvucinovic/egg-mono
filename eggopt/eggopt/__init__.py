"""Egg-integrated optimization algorithms."""

from .gepa import (
    CandidateMutation,
    CandidateMutations,
    EggflowGEPAAdapter,
    EggthreadsCandidateProposer,
    EggthreadsReflectionLM,
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
