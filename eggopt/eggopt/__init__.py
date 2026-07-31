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
    ACTOR_INSTRUCTIONS,
    COMMITTED_PLAN,
    PHYSICS_ACTOR_SYSTEM_PROMPT,
    WORLD_MODEL_TEMPLATE,
    PhysicsCritic,
    PhysicsResult,
    PhysicsStrategy,
    actor_backtest,
    actor_commit,
    actor_plan,
    canonical_plan,
    load_committed_plan,
    physics_actor_system_prompt,
    run_physics,
    write_actor_files,
)
from .recovery import InteractionRecovery, InteractionRecoveryError
from .thread_tool import ThreadTool, ThreadToolFile, ThreadToolResult

__all__ = [  # noqa: RUF022
    "ActorCritic",
    "ActorCriticResult",
    "ACTOR_INSTRUCTIONS",
    "Agent",
    "Critique",
    "COMMITTED_PLAN",
    "Evaluation",
    "GEPA",
    "GEPAConfig",
    "GEPAResult",
    "InteractionRecovery",
    "InteractionRecoveryError",
    "MutatorInput",
    "PHYSICS_ACTOR_SYSTEM_PROMPT",
    "WORLD_MODEL_TEMPLATE",
    "PhysicsCritic",
    "PhysicsResult",
    "PhysicsStrategy",
    "SelectParents",
    "ThreadTool",
    "ThreadToolFile",
    "ThreadToolResult",
    "actor_backtest",
    "actor_commit",
    "actor_plan",
    "canonical_plan",
    "current_evaluation",
    "current_operation",
    "optimize_anything",
    "load_committed_plan",
    "plan_optimization",
    "physics_actor_system_prompt",
    "run_physics",
    "write_actor_files",
]
