"""Small, durable optimization interfaces built on Eggflow and Eggthreads."""

from .actor_critic import ActorCritic, ActorCriticResult, Agent
from .context import current_evaluation, current_operation
from .evaluation import Evaluation
from .gepa import (
    GEPA,
    GEPAConfig,
    GEPAResult,
    MutatorInput,
    SelectParents,
    optimize_anything,
    plan_optimization,
)
from .physics import PhysicsEffect, PhysicsResult, PhysicsStrategy, run_physics
from .recovery import InteractionRecovery, InteractionRecoveryError
from .thread_tool import ThreadTool

__all__ = [  # noqa: RUF022
    "ActorCritic",
    "ActorCriticResult",
    "Agent",
    "Evaluation",
    "GEPA",
    "GEPAConfig",
    "GEPAResult",
    "InteractionRecovery",
    "InteractionRecoveryError",
    "MutatorInput",
    "PhysicsEffect",
    "PhysicsResult",
    "PhysicsStrategy",
    "SelectParents",
    "ThreadTool",
    "current_evaluation",
    "current_operation",
    "optimize_anything",
    "plan_optimization",
    "run_physics",
]
