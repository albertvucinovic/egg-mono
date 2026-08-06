from __future__ import annotations

from typing import Any

from ..modes import LATENT_VERIFIED

MODE = LATENT_VERIFIED


def strategy(**kwargs: Any):
    """Build PhysicsStrategy in latent and public-state verified mode."""

    from ..strategy import PhysicsStrategy

    return PhysicsStrategy.configured(
        latent=MODE.latent,
        verified=MODE.verified,
        planner=MODE.planner,
        **kwargs,
    )

__all__ = ["MODE", "strategy"]
