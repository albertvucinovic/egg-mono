"""Small, durable optimization interfaces built on Eggflow and Eggthreads."""

from .actor_critic import ActorCritic, ActorCriticResult, Agent
from .context import current_evaluation
from .evaluation import Evaluation
from .gepa import (
    GEPA,
    GEPAConfig,
    GEPAResult,
    Mutator,
    SelectParents,
    optimize_anything,
    plan_optimization,
)

__all__ = [
    "ActorCritic",
    "ActorCriticResult",
    "Agent",
    "Evaluation",
    "GEPA",
    "GEPAConfig",
    "GEPAResult",
    "Mutator",
    "SelectParents",
    "current_evaluation",
    "optimize_anything",
    "plan_optimization",
]
