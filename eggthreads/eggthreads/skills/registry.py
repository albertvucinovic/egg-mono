from __future__ import annotations

"""Packaged skill document registry."""

from dataclasses import dataclass
from importlib import resources
from typing import List


@dataclass(frozen=True)
class Skill:
    name: str
    title: str
    description: str
    package_resource: str


def _skill_resource_names() -> List[str]:
    try:
        root = resources.files(__package__)
        names = [item.name for item in root.iterdir() if item.name.endswith(".md") and item.is_file()]
    except Exception:
        names = []
    return sorted(names)


def _read_resource_text(name: str) -> str:
    return resources.files(__package__).joinpath(name).read_text(encoding="utf-8")


def _plain_line(line: str) -> str:
    return line.strip().lstrip(">").strip()


def _parse_skill(resource_name: str, text: str) -> Skill:
    name = resource_name.rsplit(".", 1)[0].strip().lower()
    title = name
    description = ""
    lines = text.splitlines()
    for idx, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("# "):
            title = line[2:].strip() or title
            continue
        if line.startswith(">"):
            parts = [_plain_line(line)]
            for continuation in lines[idx + 1:]:
                continuation = continuation.strip()
                if not continuation.startswith(">"):
                    break
                part = _plain_line(continuation)
                if part:
                    parts.append(part)
            description = " ".join(parts).strip()
            break
        if not line.startswith("#") and not line.startswith("```"):
            description = _plain_line(line)
            break
    return Skill(
        name=name,
        title=title,
        description=description,
        package_resource=resource_name,
    )


def list_skills() -> List[Skill]:
    skills: List[Skill] = []
    for resource_name in _skill_resource_names():
        try:
            skills.append(_parse_skill(resource_name, _read_resource_text(resource_name)))
        except Exception:
            continue
    return skills


def get_skill(name: str) -> Skill:
    key = (name or "").strip().lower()
    for skill in list_skills():
        if skill.name == key:
            return skill
    raise KeyError(f"Unknown skill: {name}")


def load_skill_text(name: str) -> str:
    skill = get_skill(name)
    return _read_resource_text(skill.package_resource)


def _render_skill_index_for(skills: List[Skill], *, heading: str = "### AVAILABLE SKILLS") -> str:
    if not skills:
        return ""
    lines = [heading, "Use the `skill` tool to load the full document when useful."]
    for skill in skills:
        desc = f" — {skill.description}" if skill.description else ""
        lines.append(f"- `{skill.name}`: {skill.title}{desc}")
    return "\n".join(lines)


def render_skill_index() -> str:
    return _render_skill_index_for(list_skills())


def search_skills(query: str) -> List[Skill]:
    needle = (query or "").strip().lower()
    if not needle:
        return list_skills()
    matches: List[Skill] = []
    for skill in list_skills():
        try:
            text = load_skill_text(skill.name)
        except Exception:
            text = ""
        haystack = "\n".join([skill.name, skill.title, skill.description, text]).lower()
        if needle in haystack:
            matches.append(skill)
    return matches


def render_skill_tool_output(name: str | None = None, *, query: str | None = None) -> str:
    """Return skill list or one skill document for the LLM/user-facing tool."""

    key = (name or "").strip().lower()
    if not key:
        search = (query or "").strip()
        if search:
            matches = search_skills(search)
            if not matches:
                return f"No skills matched query: {search}"
            return _render_skill_index_for(matches, heading=f"### SKILL SEARCH RESULTS: {search}")
        index = render_skill_index()
        return index or "No packaged skills available."
    try:
        skill = get_skill(key)
        text = load_skill_text(skill.name)
    except KeyError:
        available = ", ".join(skill.name for skill in list_skills()) or "none"
        return f"Error: unknown skill {name!r}. Available skills: {available}."
    return f"# Skill: {skill.name}\n\n{text}"
