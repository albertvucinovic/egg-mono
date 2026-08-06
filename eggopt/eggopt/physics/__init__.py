"""Git-backed scientific-discovery strategy."""

from .critic import PhysicsCritic
from .instruments import (
    ACTOR_INSTRUCTIONS,
    PLAN_TEMPLATE,
    WORLD_MODEL_TEMPLATE,
    write_actor_files,
)
from .latent_critic import LatentPhysicsCritic
from .lifecycle import TerminalOutcome
from .modes import LATENT, LATENT_VERIFIED, VERIFIED, PhysicsMode, physics_mode
from .planning import PLAN, canonical_plan, load_plan
from .strategy import (
    PHYSICS_ACTOR_SYSTEM_PROMPT,
    PhysicsResult,
    PhysicsStrategy,
    physics_actor_system_prompt,
    run_physics,
)
from .systemprompt import strategy_system_prompt

__all__ = [
    "ACTOR_INSTRUCTIONS",
    "LATENT",
    "LATENT_VERIFIED",
    "PHYSICS_ACTOR_SYSTEM_PROMPT",
    "PLAN",
    "PLAN_TEMPLATE",
    "VERIFIED",
    "WORLD_MODEL_TEMPLATE",
    "LatentPhysicsCritic",
    "PhysicsCritic",
    "PhysicsMode",
    "PhysicsResult",
    "PhysicsStrategy",
    "TerminalOutcome",
    "canonical_plan",
    "load_plan",
    "physics_actor_system_prompt",
    "physics_mode",
    "run_physics",
    "strategy_system_prompt",
    "write_actor_files",
]
