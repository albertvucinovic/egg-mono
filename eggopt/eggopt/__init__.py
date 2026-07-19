"""Domain-neutral optimization values and Producer composition."""

from .core import (
    Advance,
    Candidate,
    CaseEvidence,
    Feedback,
    FunctionProducer,
    JSONValue,
    Metric,
    Observation,
    Producer,
    Proposal,
    Stop,
    StrategyDecision,
    StrategyInput,
)
from .gepa import GEPAState, GEPAStrategy
from .physics import PhysicsState, PhysicsStrategy

__all__ = [
    "Advance",
    "Candidate",
    "CaseEvidence",
    "Feedback",
    "FunctionProducer",
    "GEPAState",
    "GEPAStrategy",
    "JSONValue",
    "Metric",
    "Observation",
    "PhysicsState",
    "PhysicsStrategy",
    "Producer",
    "Proposal",
    "Stop",
    "StrategyDecision",
    "StrategyInput",
]
