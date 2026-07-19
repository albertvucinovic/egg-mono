"""Composable, domain-neutral optimization primitives."""

from .core import (
    Candidate,
    FunctionProducer,
    Observation,
    Producer,
    Proposal,
    Strategy,
    StrategyDecision,
    StrategyInput,
)
from .strategies import GEPAState, GEPAStrategy, PhysicsState, PhysicsStrategy

__all__ = [
    "Candidate",
    "FunctionProducer",
    "GEPAState",
    "GEPAStrategy",
    "Observation",
    "PhysicsState",
    "PhysicsStrategy",
    "Producer",
    "Proposal",
    "Strategy",
    "StrategyDecision",
    "StrategyInput",
]
