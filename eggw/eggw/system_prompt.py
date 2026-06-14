"""System prompt helpers for EggW root threads."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_SYSTEM_PROMPT = "You are a helpful assistant."


def _append_skill_index(prompt: str) -> str:
    try:
        from eggthreads.skills import render_skill_index

        skill_index = render_skill_index().strip()
    except Exception:
        skill_index = ""
    if skill_index and skill_index not in prompt:
        return f"{prompt}\n\n{skill_index}".strip()
    return prompt.strip()


def load_system_prompt() -> str:
    """Load the Egg system prompt, including the skill index when available."""
    try:
        from egg.utils import get_system_prompt

        prompt = get_system_prompt().strip()
        if prompt:
            return prompt
    except Exception:
        pass

    candidates = [
        Path.cwd() / "egg" / "egg" / "systemPrompt",
        Path.cwd() / "systemPrompt",
        Path(__file__).resolve().parents[2] / "egg" / "egg" / "systemPrompt",
    ]
    for candidate in candidates:
        try:
            prompt = candidate.read_text(encoding="utf-8").strip()
        except Exception:
            continue
        if prompt:
            return _append_skill_index(prompt)
    return _append_skill_index(DEFAULT_SYSTEM_PROMPT)


def _has_system_message(db: Any, thread_id: str) -> bool:
    try:
        rows = db.conn.execute(
            "SELECT payload_json FROM events WHERE thread_id=? AND type='msg.create'",
            (thread_id,),
        ).fetchall()
    except Exception:
        return False
    for row in rows:
        try:
            payload_json = row[0]
            payload = json.loads(payload_json) if isinstance(payload_json, str) else (payload_json or {})
        except Exception:
            payload = {}
        if isinstance(payload, dict) and payload.get("role") == "system":
            return True
    return False


def append_root_system_prompt(db: Any, thread_id: str) -> bool:
    """Append the loaded system prompt to a new root thread once."""
    if _has_system_message(db, thread_id):
        return False

    from eggthreads import append_message, create_snapshot

    append_message(db, thread_id, "system", load_system_prompt())
    create_snapshot(db, thread_id)
    return True
