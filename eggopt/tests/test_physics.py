from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
from eggopt.physics import canonical_plan
from eggopt.physics.theory import evaluator_script, parse_evaluator_output

from eggflow import Task
from eggopt import Agent, PhysicsStrategy, physics_actor_system_prompt
from eggthreads import (
    ThreadsDB,
    ToolRegistry,
    list_children_with_meta,
    list_root_threads,
)

MODEL = """
def step_a(state, action):
    return {"position": state["position"] + 1, "legal_actions": [1]}
def reward_a(state):
    return float(state["position"] >= 2)
def step_b(state, action):
    return {"position": state["position"] + (1 if state["position"] == 0 else 2), "legal_actions": [1]}
def reward_b(state):
    return float(state["position"] >= 3)
"""


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
        if self.edit:
            self.edit(self.calls)
        yield {
            "type": "message",
            "role": "assistant",
            "content": next(self.replies),
            "stop_reason": "end_turn",
        }


@dataclass
class Value(Task):
    value: object

    def run(self):
        return self.value


def git(path, *args):
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, text=True, capture_output=True
    ).stdout.strip()


def strategy(workspace, edit, *, replies=("ready",), tools=None, execute=None):
    llm = ScriptedLLM(replies, edit)
    tools = tools or Agent(object(), {"role": "default"}).tools
    actor = Agent(
        llm,
        {"role": "physics-actor"},
        tools=tools,
        auto_approve_tools=True,
        allowed_tools=frozenset({"bash", "python_exec"}),
        system_prompt=physics_actor_system_prompt("Toy domain."),
    )
    return (
        PhysicsStrategy(
            actor=actor,
            observe=lambda **_: Value({"position": 0, "legal_actions": [1]}),
            execute=execute or (lambda intent, **_: Value(intent["prediction"]["a"])),
            is_goal=lambda state: state["position"] >= 2,
            identity={"domain": "toy"},
            domain_information="State has position and legal_actions.",
            max_depth=4,
        ),
        llm,
    )


def write_plan(workspace, plan, message="actor theory and plan"):
    (workspace / "world_model.py").write_text(MODEL)
    (workspace / "committed-plan.json").write_text(json.dumps(plan))
    git(workspace, "add", "-A")
    git(workspace, "commit", "-m", message)


def experiment_plan():
    request = {
        "source": MODEL,
        "timeline": [{"position": 0, "legal_actions": [1]}],
        "legal_actions_key": "legal_actions",
        "max_depth": 4,
        "max_nodes": 100,
    }
    result = run_evaluator(request)
    return next(
        plan for plan in result["planning"]["plans"] if plan["purpose"] == "experiment"
    )


def run_evaluator(request):
    completed = subprocess.run(
        ["python", "-c", evaluator_script(request)],
        text=True,
        capture_output=True,
        check=True,
    )
    return parse_evaluator_output(completed.stdout)


def test_generic_evaluator_reports_all_models_and_multistep_discrimination():
    result = run_evaluator(
        {
            "source": MODEL,
            "timeline": [{"position": 0, "legal_actions": [1]}],
            "legal_actions_key": "legal_actions",
            "max_depth": 4,
            "max_nodes": 100,
        }
    )

    assert set(result["backtest"]["models"]) == {"a", "b"}
    experiment = result["planning"]["discrimination_plans"][0]
    assert len(experiment["plan"]) == 2
    assert (
        experiment["plan"][0]["prediction"]["a"]
        == experiment["plan"][0]["prediction"]["b"]
    )
    assert (
        experiment["plan"][1]["prediction"]["a"]
        != experiment["plan"][1]["prediction"]["b"]
    )


def test_planner_reports_models_even_when_none_survive():
    timeline = [
        {"position": 0, "legal_actions": [1]},
        {
            "state": {"position": 0, "legal_actions": [1]},
            "action": {"action": 1},
            "next_state": {"position": 99, "legal_actions": [1]},
        },
    ]
    result = run_evaluator(
        {
            "source": MODEL,
            "timeline": timeline,
            "legal_actions_key": "legal_actions",
            "max_depth": 4,
            "max_nodes": 100,
        }
    )

    assert result["backtest"]["surviving_models"] == []
    assert set(result["planning"]["goal_plans"]) == {"a", "b"}
    assert result["planning"]["discrimination_plans"]


def test_physics_uses_critic_thread_python_exec_and_executes_until_branch(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    workspace = tmp_path / "run" / "workspace" / "innerContext"
    calls = []
    tools = ToolRegistry()

    def python_exec(arguments, context):
        calls.append(context.thread_id)
        completed = subprocess.run(
            ["python", "-c", arguments["script"]],
            cwd=Path(context.db.path).parent.parent / "workspace" / "critic-repository",
            text=True,
            capture_output=True,
            check=True,
        )
        return completed.stdout

    tools.register(
        "bash", "unused", {"type": "object", "properties": {}}, lambda _args: ""
    )
    tools.register(
        "python_exec",
        "sandbox evaluator",
        {"type": "object", "properties": {"script": {"type": "string"}}},
        python_exec,
        accepts_context=True,
    )
    plan = experiment_plan()

    def edit(_call):
        write_plan(workspace, plan)

    observed = []

    def execute(intent, **_):
        next_state = intent["prediction"]["a"]
        observed.append(next_state)
        return Value(next_state)

    physics, _actor = strategy(workspace, edit, tools=tools, execute=execute)
    result = physics.run(run_dir="run", max_cycles=1)

    assert result.accepted is False
    assert len(observed) == 2
    assert result.feedback.endswith("models_discriminated.")
    assert calls == [result.critic_thread_id]
    db = ThreadsDB(tmp_path / "run" / ".egg" / "threads.sqlite")
    try:
        root = list_root_threads(db)[0]
        critic = list_children_with_meta(db, root)
        assert [name for _, name, *_ in critic] == ["Critic"]
        assert critic[0][0] == result.critic_thread_id
        assert [name for _, name, *_ in list_children_with_meta(db, critic[0][0])] == [
            "Actor"
        ]
    finally:
        db.close()


def test_dirty_repository_rejected_then_fixed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    workspace = tmp_path / "run" / "workspace" / "innerContext"
    plan = canonical_plan(
        {
            "purpose": "goal",
            "models": ["a"],
            "intents": [
                {
                    "action": 1,
                    "prediction": {"a": {"position": 1, "legal_actions": [1]}},
                },
                {
                    "action": 1,
                    "prediction": {"a": {"position": 2, "legal_actions": [1]}},
                },
            ],
        }
    )

    def edit(call):
        if call == 1:
            write_plan(workspace, plan)
            (workspace / "dirty.txt").write_text("dirty")
        else:
            (workspace / ".gitignore").write_text("scratch/\ndirty.txt\n")
            git(workspace, "add", "-A")
            git(workspace, "commit", "-m", "ignore scratch")

    physics, actor = strategy(workspace, edit, replies=("ready", "fixed"))
    result = physics.run(run_dir="run", max_cycles=2)

    assert result.accepted is True
    assert actor.calls == 2
    assert not git(workspace, "status", "--short")


def test_deleted_repo_restores_history_and_authoritative_state(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    workspace = tmp_path / "run" / "workspace" / "innerContext"
    plan = experiment_plan()

    def edit(call):
        if call == 1:
            write_plan(workspace, plan, "first plan")
        elif call == 2:
            import shutil

            shutil.rmtree(workspace / ".git")
        else:
            assert (workspace / ".git").exists()
            (workspace / "restored.txt").write_text("restored")
            git(workspace, "add", "-A")
            git(workspace, "commit", "-m", "restored plan")

    physics, actor = strategy(workspace, edit, replies=("ready", "reset", "restored"))

    def wrong_execute(intent, **_):
        return Value({"position": 999, "legal_actions": [1]})

    object.__setattr__(physics, "execute", wrong_execute)
    physics.run(run_dir="run", max_cycles=3, max_actions=10)

    assert actor.calls == 3
    assert (workspace / ".git").exists()
    assert "[physics]" in git(workspace, "log", "--format=%s", "-2")


def test_actor_system_prompt_is_domain_extensible():
    prompt = physics_actor_system_prompt("Color grids and ARC actions.")
    assert "Git repository" in prompt
    assert "non-empty plan" in prompt
    assert "Color grids" in prompt


def test_physics_requires_domain_ports():
    actor = Agent(object(), {"role": "actor"})
    with pytest.raises(TypeError, match="observe"):
        PhysicsStrategy(
            actor=actor,
            observe="bad",
            execute=lambda **_: Value({}),
            is_goal=lambda _: False,
            identity={},
        )
