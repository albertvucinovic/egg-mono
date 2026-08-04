"""Git-backed scientific-discovery strategy."""

from .critic import PhysicsCritic
from .instruments import (
    ACTOR_INSTRUCTIONS,
    PLAN_TEMPLATE,
    WORLD_MODEL_TEMPLATE,
    write_actor_files,
)
from .lifecycle import TerminalOutcome
from .planning import PLAN, canonical_plan, load_plan
from .strategy import (
    PHYSICS_ACTOR_SYSTEM_PROMPT,
    PhysicsResult,
    PhysicsStrategy,
    physics_actor_system_prompt,
    run_physics,
)

__all__ = [
    "ACTOR_INSTRUCTIONS",
    "PHYSICS_ACTOR_SYSTEM_PROMPT",
    "PLAN",
    "PLAN_TEMPLATE",
    "WORLD_MODEL_TEMPLATE",
    "PhysicsCritic",
    "PhysicsResult",
    "PhysicsStrategy",
    "TerminalOutcome",
    "canonical_plan",
    "load_plan",
    "physics_actor_system_prompt",
    "run_physics",
    "write_actor_files",
]
