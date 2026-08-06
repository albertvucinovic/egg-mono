from __future__ import annotations

import json
from pathlib import Path

from .systemprompt import strategy_system_prompt
from .theory import evaluator_source

WORLD_MODEL_TEMPLATE = '''"""Competing hypotheses for the observed world."""


def step_1(state, action):
    """Return the complete predicted next public state."""
    raise NotImplementedError


# Normally define reward_1(state) so plan.py can search model 1 productively.
'''

PLAN_TEMPLATE = "[]\n"

LATENT_WORLD_MODEL_TEMPLATE = '''"""Executable latent-state hypotheses."""


def encode_main(evidence):
    """Return the current finite JSON latent state from canonical evidence."""
    raise NotImplementedError


def step_main(z, action):
    """Return the predicted next latent state."""
    raise NotImplementedError
'''

LATENT_VERIFIED_WORLD_MODEL_TEMPLATE = LATENT_WORLD_MODEL_TEMPLATE + '''


def observe_main(z):
    """Return the complete public state represented by the latent state."""
    raise NotImplementedError
'''

LATENT_PLAN_TEMPLATE = '''{
  "actions": [],
  "model": "main"
}
'''

ACTOR_INSTRUCTIONS = strategy_system_prompt()

BACKTEST_WRAPPER = '''"""Backtest the Actor's executable world-model hypotheses."""

from plan import run_backtest


if __name__ == "__main__":
    run_backtest()
'''

_RESERVED_DOMAIN_FILENAMES = frozenset(
    {
        ".gitignore",
        "INSTRUCTIONS.md",
        "backtest-report.json",
        "backtest.py",
        "canonical-input.json",
        "commit.py",
        "physics-config.json",
        "physics-mode.json",
        "plan-report.json",
        "plan.json",
        "plan.py",
        "trusted-report.json",
        "world_model.py",
    }
)


def validate_domain_files(value) -> tuple[tuple[str, str], ...]:
    """Validate root-level helper files supplied by a Physics domain."""

    if not isinstance(value, tuple):
        raise TypeError("domain_files must be a finite tuple")
    names: set[str] = set()
    for item in value:
        if (
            not isinstance(item, tuple)
            or len(item) != 2
            or not all(isinstance(part, str) for part in item)
        ):
            raise TypeError("domain_files entries must be (name, text) tuples")
        name, _content = item
        path = Path(name)
        if (
            not name
            or path.is_absolute()
            or len(path.parts) != 1
            or path.name in {".", ".."}
            or "/" in name
            or "\\" in name
            or name in names
        ):
            raise ValueError("domain_files names must be unique root-level filenames")
        if name in _RESERVED_DOMAIN_FILENAMES:
            raise ValueError(f"domain_files cannot replace reserved file {name!r}")
        names.add(name)
    return value


def instrument_files(
    *,
    planner: bool = True,
    planner_actions,
    default_search_depth: int,
    default_max_nodes: int,
) -> dict[str, str]:
    config = json.dumps(
        _instrument_configuration(
            planner_actions=planner_actions,
            default_search_depth=default_search_depth,
            default_max_nodes=default_max_nodes,
        ),
        indent=2,
        sort_keys=True,
    ) + "\n"
    if not planner:
        return {"physics-config.json": config}
    return {
        "backtest.py": BACKTEST_WRAPPER,
        "physics-config.json": config,
        "plan.py": evaluator_source(),
    }


def _instrument_configuration(
    *,
    planner_actions=(),
    default_search_depth: int = 8,
    default_max_nodes: int = 10_000,
) -> dict[str, object]:
    return {
        "default_search_depth": default_search_depth,
        "default_max_nodes": default_max_nodes,
        "planner_actions": list(planner_actions),
    }


def write_actor_files(
    workspace: str | Path,
    timeline,
    domain_information: str = "",
    *,
    instructions: str = ACTOR_INSTRUCTIONS,
    planner: bool = True,
    mode: object | None = None,
    domain_files=(),
    planner_actions=(),
    default_search_depth: int = 8,
    default_max_nodes: int = 10_000,
) -> None:
    workspace = Path(workspace)
    domain_files = validate_domain_files(domain_files)
    workspace.mkdir(parents=True, exist_ok=True)
    instructions = instructions.strip()
    if domain_information.strip():
        instructions += "\n## Domain information\n\n" + domain_information.strip() + "\n"
    _write_if_missing(workspace / "INSTRUCTIONS.md", instructions)
    _write_json(workspace / "canonical-input.json", {"timeline": timeline})
    _write_if_missing(
        workspace / ".gitignore",
        "scratch/\n__pycache__/\n*.pyc\n.physics-evaluation/\n",
    )
    latent = bool(getattr(mode, "latent", False))
    verified = bool(getattr(mode, "verified", True))
    world_model = (
        LATENT_VERIFIED_WORLD_MODEL_TEMPLATE
        if latent and verified
        else (LATENT_WORLD_MODEL_TEMPLATE if latent else WORLD_MODEL_TEMPLATE)
    )
    _write_if_missing(workspace / "world_model.py", world_model)
    _write_if_missing(
        workspace / "plan.json", LATENT_PLAN_TEMPLATE if latent else PLAN_TEMPLATE
    )
    if mode is not None:
        _write_if_missing(
            workspace / "physics-mode.json",
            json.dumps(
                {
                    "latent": mode.latent,
                    "verified": mode.verified,
                    "planner": mode.planner,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
    for name, content in instrument_files(
        planner=planner,
        planner_actions=planner_actions,
        default_search_depth=default_search_depth,
        default_max_nodes=default_max_nodes,
    ).items():
        _write_if_missing(workspace / name, content)
    for name, content in domain_files:
        _write_if_missing(workspace / name, content)


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def _write_if_missing(path, content):
    if not path.exists():
        path.write_text(content)


def ensure_evaluator_ignore(workspace: str | Path) -> None:
    path = Path(workspace) / ".gitignore"
    lines = path.read_text().splitlines() if path.is_file() else []
    if ".physics-evaluation/" not in lines:
        lines.append(".physics-evaluation/")
        path.write_text("\n".join(lines) + "\n")


__all__ = [
    "ACTOR_INSTRUCTIONS",
    "LATENT_PLAN_TEMPLATE",
    "LATENT_VERIFIED_WORLD_MODEL_TEMPLATE",
    "LATENT_WORLD_MODEL_TEMPLATE",
    "PLAN_TEMPLATE",
    "WORLD_MODEL_TEMPLATE",
    "ensure_evaluator_ignore",
    "instrument_files",
    "validate_domain_files",
    "write_actor_files",
]
