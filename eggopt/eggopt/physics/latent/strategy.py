from __future__ import annotations

from typing import Any

from ..modes import LATENT

MODE = LATENT


def strategy(**kwargs: Any):
    """Build PhysicsStrategy in trusted latent-state mode."""

    from ..strategy import PhysicsStrategy

    return PhysicsStrategy.configured(
        latent=MODE.latent,
        verified=MODE.verified,
        planner=MODE.planner,
        **kwargs,
    )


__all__ = ["MODE", "strategy"]
