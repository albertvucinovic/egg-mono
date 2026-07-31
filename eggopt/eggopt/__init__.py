"""Small, durable optimization interfaces built on Eggflow and Eggthreads."""

from .actor_critic import ActorCritic, ActorCriticResult, Agent, Critique
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
from .physics import (
    PHYSICS_ACTOR_SYSTEM_PROMPT,
    PhysicsResult,
    PhysicsStrategy,
    physics_actor_system_prompt,
    run_physics,
)
from .recovery import InteractionRecovery, InteractionRecoveryError
from .thread_tool import ThreadTool

__all__ = [  # noqa: RUF022
    "ActorCritic",
    "ActorCriticResult",
    "Agent",
    "Critique",
    "Evaluation",
    "GEPA",
    "GEPAConfig",
    "GEPAResult",
    "InteractionRecovery",
    "InteractionRecoveryError",
    "MutatorInput",
    "PHYSICS_ACTOR_SYSTEM_PROMPT",
    "PhysicsResult",
    "PhysicsStrategy",
    "SelectParents",
    "ThreadTool",
    "current_evaluation",
    "current_operation",
    "optimize_anything",
    "plan_optimization",
    "physics_actor_system_prompt",
    "run_physics",
]
