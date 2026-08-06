from __future__ import annotations

from importlib import resources

from .modes import VERIFIED, PhysicsMode


def strategy_system_prompt(mode: PhysicsMode = VERIFIED) -> str:
    """Load the system prompt owned by one symmetric Physics strategy package."""

    package = (
        "eggopt.physics.latent"
        if mode.latent and not mode.verified
        else (
            "eggopt.physics.latent-verified"
            if mode.latent
            else "eggopt.physics.verified"
        )
    )
    return resources.files(package).joinpath("systemprompt.md").read_text().strip()


def physics_actor_system_prompt(
    domain_information: str = "", *, mode: PhysicsMode = VERIFIED
) -> str:
    """Return one Physics strategy prompt followed by domain-supplied guidance."""

    prompt = strategy_system_prompt(mode)
    domain_information = str(domain_information or "").strip()
    if domain_information:
        prompt += "\n\n## Domain information\n\n" + domain_information
    return prompt


__all__ = ["physics_actor_system_prompt", "strategy_system_prompt"]
