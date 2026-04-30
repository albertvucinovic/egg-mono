"""Compatibility wrapper for the canonical eggthreads skill registry."""

from eggthreads.skills.registry import Skill, get_skill, list_skills, load_skill_text, render_skill_index, render_skill_tool_output, search_skills

__all__ = [
    "Skill",
    "get_skill",
    "list_skills",
    "load_skill_text",
    "render_skill_index",
    "render_skill_tool_output",
    "search_skills",
]
