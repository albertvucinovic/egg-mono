from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from eggflow import Task
from eggopt import Agent, Critique, PhysicsStrategy, physics_actor_system_prompt
from eggthreads import (
    ThreadsDB,
    list_children_with_meta,
    list_root_threads,
    list_threads,
)


class ScriptedLLM:
    current_model_key = "test-model"

    def __init__(self, replies, edit=None):
        self.replies = iter(replies)
        self.edit = edit
        self.calls = 0

    def set_model(self, key):
        self.current_model_key = key

    def set_model_with_config(self, key, _config):
        self.current_model_key = key

    async def astream_chat(self, _messages, **_kwargs):
        self.calls += 1
        if self.edit is not None:
            self.edit(self.calls)
        yield {
            "type": "message",
            "role": "assistant",
            "content": next(self.replies),
            "stop_reason": "end_turn",
        }


def git(path, *args):
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


@dataclass
class Prepare(Task):
    workspace: str | None = None

    def get_cache_key(self):
        return "test.physics.prepare.v1"

    def run(self):
        workspace = Path(self.workspace)
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "INSTRUCTIONS.md").write_text("Study the toy world.\n")
        (workspace / "state.json").write_text('{"position": 0}\n')
        (workspace / ".gitignore").write_text("scratch/\n")


@dataclass
class Review(Task):
    workspace: str | None = None
    head: str | None = None
    visits: list[str] | None = None
    accept: bool = True

    def get_cache_key(self):
        return "test.physics.review.v1"

    def run(self):
        plan = Path(self.workspace, "committed-plan.json")
        if not plan.is_file():
            return Critique.revise("committed-plan.json is missing")
        if self.visits is not None:
            self.visits.append(self.head)
        trusted = Path(self.workspace, ".trusted")
        trusted.mkdir(exist_ok=True)
        (trusted / "report.json").write_text('{"reviewed": true}\n')
        if self.accept:
            return Critique.accept(
                {"stopping_reason": "won", "head": self.head}, "Game won."
            )
        return Critique.revise("Reality added new evidence; revise the theory.")


def strategy(tmp_path, *, replies=("ready",), edit=None, review=None):
    actor = ScriptedLLM(replies, edit=edit)
    return (
        PhysicsStrategy(
            actor=Agent(
                actor,
                {"role": "physics-actor"},
                auto_approve_tools=True,
                allowed_tools=frozenset({"bash", "python_exec"}),
                system_prompt=physics_actor_system_prompt("Toy domain."),
            ),
            prepare=lambda **_: Prepare(),
            critic=review or Review(),
            identity={"domain": "toy"},
        ),
        actor,
    )


def test_physics_actor_critic_accepts_clean_committed_head(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    workspace = tmp_path / "run" / "workspace" / "innerContext"
    visits = []

    def edit(_call):
        (workspace / "world_model.py").write_text(
            "def step_1(state, action): return state\n"
        )
        (workspace / "committed-plan.json").write_text(
            '{"intents":[{"action":1,"prediction":{"1":{"position":1}}}]}\n'
        )
        git(workspace, "add", "-A")
        git(workspace, "commit", "-m", "actor theory and plan")

    physics, actor = strategy(tmp_path, edit=edit, review=Review(visits=visits))
    result = physics.run(run_dir="run", max_actions=5, max_cycles=2)

    assert result.accepted is True
    assert result.stopping_reason == "won"
    assert visits == [result.value["head"]]
    assert result.head == git(workspace, "rev-parse", "HEAD")
    assert actor.calls == 1
    critic_copy = tmp_path / "run" / "workspace" / "critic-repository"
    assert git(critic_copy, "log", "--format=%s", "-1") == (
        "[physics] trusted Critic result after " + visits[0][:12]
    )

    db = ThreadsDB(tmp_path / "run" / ".egg" / "threads.sqlite")
    try:
        root = list_root_threads(db)[0]
        assert (
            next(t.name for t in list_threads(db) if t.thread_id == root) == "Physics"
        )
        critic = list_children_with_meta(db, root)
        assert [name for _, name, *_ in critic] == ["Critic"]
        assert [name for _, name, *_ in list_children_with_meta(db, critic[0][0])] == [
            "Actor"
        ]
    finally:
        db.close()


def test_dirty_repository_is_rejected_then_actor_can_fix_it(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    workspace = tmp_path / "run" / "workspace" / "innerContext"

    def edit(call):
        if call == 1:
            (workspace / "committed-plan.json").write_text(
                '{"intents":[{"action":1}]}\n'
            )
            git(workspace, "add", "-A")
            git(workspace, "commit", "-m", "actor plan")
            (workspace / "forgotten.txt").write_text("dirty\n")
        else:
            (workspace / ".gitignore").write_text("scratch/\nforgotten.txt\n")
            git(workspace, "add", "-A")
            git(workspace, "commit", "-m", "ignore scratch")

    physics, actor = strategy(tmp_path, replies=("ready", "fixed"), edit=edit)
    result = physics.run(run_dir="run", max_cycles=2)

    assert result.accepted is True
    assert actor.calls == 2
    assert not git(workspace, "status", "--short")


def test_deleted_actor_git_is_restored_from_critic_copy(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    workspace = tmp_path / "run" / "workspace" / "innerContext"

    def edit(call):
        if call == 1:
            (workspace / "committed-plan.json").write_text(
                '{"intents":[{"action":1}]}\n'
            )
            git(workspace, "add", "-A")
            git(workspace, "commit", "-m", "first actor plan")
        else:
            (workspace / "committed-plan.json").write_text(
                '{"intents":[{"action":2}]}\n'
            )
            git(workspace, "add", "-A")
            git(workspace, "commit", "-m", "restored actor plan")

    review = Review(accept=False)
    physics, actor = strategy(
        tmp_path,
        replies=("ready", "reset", "restored"),
        edit=edit,
        review=review,
    )

    # Delete .git during the second turn to request a reset. Critic restores its copy.
    original = actor.edit

    def reset_edit(call):
        if call == 2:
            import shutil

            shutil.rmtree(workspace / ".git")
        else:
            original(call if call == 1 else 2)

    actor.edit = reset_edit
    result = physics.run(run_dir="run", max_cycles=3)

    assert result.accepted is False
    assert actor.calls == 3
    assert (workspace / ".git").exists()
    assert git(workspace, "log", "--format=%s", "-1") == "restored actor plan"


def test_restore_overlays_latest_authoritative_state(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    workspace = tmp_path / "run" / "workspace" / "innerContext"

    def edit(call):
        if call == 1:
            (workspace / "committed-plan.json").write_text(
                '{"intents":[{"action":1}]}\n'
            )
            git(workspace, "add", "-A")
            git(workspace, "commit", "-m", "actor plan")
        elif call == 2:
            import shutil

            shutil.rmtree(workspace / ".git")
        else:
            assert '"position": 7' in (workspace / "canonical-input.json").read_text()
            (workspace / "committed-plan.json").write_text(
                '{"intents":[{"action":2}]}\n'
            )
            git(workspace, "add", "-A")
            git(workspace, "commit", "-m", "rehydrated plan")

    @dataclass
    class StatefulReview(Review):
        outer_context: str | None = None

        def run(self):
            trusted = Path(self.outer_context, ".trusted")
            trusted.mkdir(parents=True, exist_ok=True)
            (trusted / "state.json").write_text(
                '{"timeline":[{"position":7}],"actions":1}\n'
            )
            return Critique.revise("New evidence; revise.")

    physics, actor = strategy(
        tmp_path,
        replies=("ready", "reset", "rehydrated"),
        edit=edit,
        review=StatefulReview(),
    )
    physics.run(run_dir="run", max_cycles=3)

    assert actor.calls == 3
    assert '"position": 7' in (workspace / "canonical-input.json").read_text()


def test_actor_system_prompt_is_extensible():
    prompt = physics_actor_system_prompt("ARC observations are color grids.")

    assert "Git repository" in prompt
    assert "non-empty plan" in prompt
    assert "delete .git" in prompt
    assert "ARC observations are color grids" in prompt


def test_physics_requires_task_contracts():
    with pytest.raises(TypeError, match="prepare"):
        PhysicsStrategy(
            actor=Agent(object(), {"role": "actor"}),
            prepare="not callable",
            critic=Review(),
            identity={"bad": True},
        )
    with pytest.raises(TypeError, match="critic"):
        PhysicsStrategy(
            actor=Agent(object(), {"role": "actor"}),
            prepare=lambda **_: Prepare(),
            critic="not a task",
            identity={"bad": True},
        )
