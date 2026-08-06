from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PhysicsMode:
    """The three independent behavioral choices of a Physics strategy."""

    latent: bool
    verified: bool
    planner: bool

    def __post_init__(self) -> None:
        for name in ("latent", "verified", "planner"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be boolean")

    @property
    def name(self) -> str:
        for name, values in {
            "latent": (True, False, False),
    "latent-verified": (True, True, False),
            "verified": (False, True, True),
        }.items():
            if (self.latent, self.verified, self.planner) == values:
                return name
        return (
            f"custom(latent={self.latent},verified={self.verified},"
            f"planner={self.planner})"
        )


LATENT = PhysicsMode(latent=True, verified=False, planner=False)
LATENT_VERIFIED = PhysicsMode(latent=True, verified=True, planner=False)
VERIFIED = PhysicsMode(latent=False, verified=True, planner=True)


def physics_mode(*, latent: bool, verified: bool, planner: bool) -> PhysicsMode:
    """Return the canonical mode for a supported Physics strategy combination."""

    value = PhysicsMode(latent=latent, verified=verified, planner=planner)
    return {mode.name: mode for mode in (LATENT, LATENT_VERIFIED, VERIFIED)}.get(
        value.name, value
    )


__all__ = [
    "LATENT",
    "LATENT_VERIFIED",
    "VERIFIED",
    "PhysicsMode",
    "physics_mode",
]
