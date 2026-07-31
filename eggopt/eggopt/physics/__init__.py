"""Git-backed scientific-discovery strategy."""

from .strategy import (
    PHYSICS_ACTOR_SYSTEM_PROMPT,
    PhysicsResult,
    PhysicsStrategy,
    physics_actor_system_prompt,
    run_physics,
)

__all__ = [
    "PHYSICS_ACTOR_SYSTEM_PROMPT",
    "PhysicsResult",
    "PhysicsStrategy",
    "physics_actor_system_prompt",
    "run_physics",
]
