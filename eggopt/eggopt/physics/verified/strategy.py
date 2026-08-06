from __future__ import annotations

from typing import Any

from ..modes import VERIFIED

MODE = VERIFIED


def strategy(**kwargs: Any):
    """Build the current complete public-state Physics strategy."""

    from ..strategy import PhysicsStrategy

    return PhysicsStrategy.configured(
        latent=MODE.latent,
        verified=MODE.verified,
        planner=MODE.planner,
        **kwargs,
    )


__all__ = ["MODE", "strategy"]
