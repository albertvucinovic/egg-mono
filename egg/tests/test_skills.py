from __future__ import annotations

from egg.skills.registry import get_skill, list_skills, load_skill_text


def test_rlm_skill_is_packaged_and_loadable() -> None:
    names = {skill.name for skill in list_skills()}
    assert "rlm" in names
    assert "worker-manager" in names
    assert get_skill("rlm").title
    text = load_skill_text("rlm")
    assert "chunk_text" in text
    assert "llm_query" in text
    assert "from eggtools.rlm" not in text
    assert "special RLM runtime module" in text


def test_worker_manager_skill_is_packaged_and_loadable() -> None:
    skill = get_skill("worker-manager")
    assert "Worker Manager" in skill.title
    text = load_skill_text("worker-manager")
    assert "spawn_agent_auto" in text
    assert "commit" in text
