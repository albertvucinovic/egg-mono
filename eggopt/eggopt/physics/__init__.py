"""Git-backed scientific-discovery strategy."""

from .critic import PhysicsCritic
from .instruments import (
    ACTOR_INSTRUCTIONS,
    PROPOSED_PLANS_TEMPLATE,
    WORLD_MODEL_TEMPLATE,
    actor_backtest,
    actor_commit,
    actor_plan,
    write_actor_files,
)
from .planning import (
    COMMITTED_PLAN,
    PROPOSED_PLANS,
    canonical_plan,
    load_committed_plan,
    load_proposed_plans,
)
from .strategy import (
    PHYSICS_ACTOR_SYSTEM_PROMPT,
    PhysicsResult,
    PhysicsStrategy,
    physics_actor_system_prompt,
    run_physics,
)

__all__ = [
    "ACTOR_INSTRUCTIONS",
    "COMMITTED_PLAN",
    "PHYSICS_ACTOR_SYSTEM_PROMPT",
    "PROPOSED_PLANS",
    "PROPOSED_PLANS_TEMPLATE",
    "WORLD_MODEL_TEMPLATE",
    "PhysicsCritic",
    "PhysicsResult",
    "PhysicsStrategy",
    "actor_backtest",
    "actor_commit",
    "actor_plan",
    "canonical_plan",
    "load_committed_plan",
    "load_proposed_plans",
    "physics_actor_system_prompt",
    "run_physics",
    "write_actor_files",
]
