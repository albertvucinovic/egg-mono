from __future__ import annotations

"""Minimal packaged skill registry for Egg."""

from dataclasses import dataclass
from importlib import resources
from typing import Dict, List


@dataclass(frozen=True)
class Skill:
    name: str
    title: str
    description: str
    package_resource: str


_SKILLS: Dict[str, Skill] = {
    "rlm": Skill(
        name="rlm",
        title="RLM: persistent REPL + recursive subthreads",
        description=(
            "Use persistent Python REPL variables for large context/tool outputs, "
            "then chunk, fan out to subthreads, and synthesize compact findings."
        ),
        package_resource="rlm.md",
    ),
}


def list_skills() -> List[Skill]:
    return list(_SKILLS.values())


def get_skill(name: str) -> Skill:
    key = (name or "").strip().lower()
    if key not in _SKILLS:
        raise KeyError(f"Unknown skill: {name}")
    return _SKILLS[key]


def load_skill_text(name: str) -> str:
    skill = get_skill(name)
    return resources.files(__package__).joinpath(skill.package_resource).read_text(encoding="utf-8")
