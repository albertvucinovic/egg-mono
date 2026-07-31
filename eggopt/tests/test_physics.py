from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
from eggopt.physics import canonical_plan
from eggopt.physics.critic import (
    _evaluation_report,
    _evaluation_report_path,
    _evaluation_request_path,
    _execution_feedback,
)
from eggopt.physics.strategy import _actor_turn_prompt
from eggopt.physics.theory import (
    evaluator_file_script,
    evaluator_script,
    parse_evaluator_output,
    parse_evaluator_receipt,
)

from eggflow import Task
from eggopt import (
    ACTOR_INSTRUCTIONS,
    PHYSICS_ACTOR_SYSTEM_PROMPT,
    Agent,
    PhysicsStrategy,
    physics_actor_system_prompt,
)
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


def test_generic_evaluator_can_write_a_compactly_receipted_report(tmp_path):
    report = tmp_path / "trusted" / "report.json"
    request = {
        "source": MODEL,
        "timeline": [{"position": 0, "legal_actions": [1]}],
        "legal_actions_key": "legal_actions",
        "max_depth": 4,
        "max_nodes": 100,
        "work_dir": str(tmp_path / "work"),
        "output_path": str(report),
    }
    completed = subprocess.run(
        ["python", "-c", evaluator_script(request)],
        text=True,
        capture_output=True,
        check=True,
    )

    assert parse_evaluator_receipt(completed.stdout) == str(report)
    assert set(json.loads(report.read_text())["backtest"]["models"]) == {"a", "b"}
    assert "models" not in completed.stdout


def test_generic_evaluator_loads_large_inputs_from_workspace_files(tmp_path):
    report = tmp_path / "trusted" / "report.json"
    canonical = tmp_path / "canonical-input.json"
    canonical.write_text(
        json.dumps(
            {
                "timeline": [
                    {
                        "position": 0,
                        "legal_actions": [1],
                        "irrelevant": "x" * 200_000,
                    }
                ]
            }
        )
    )
    (tmp_path / "world_model.py").write_text(MODEL)
    request = tmp_path / "trusted" / "request.json"
    request.parent.mkdir()
    request.write_text(
        json.dumps(
            {
                "source_path": "world_model.py",
                "timeline_path": "canonical-input.json",
                "legal_actions_key": "legal_actions",
                "max_depth": 4,
                "max_nodes": 100,
                "work_dir": "trusted/work",
                "output_path": "trusted/report.json",
            }
        )
    )

    script = evaluator_file_script("trusted/request.json")
    completed = subprocess.run(
        ["python", "-c", script],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=True,
    )

    assert len(script) < 10_000
    assert "x" * 1_000 not in script
    assert parse_evaluator_receipt(completed.stdout) == "trusted/report.json"
    assert set(json.loads(report.read_text())["backtest"]["models"]) == {"a", "b"}


def test_file_evaluator_script_stays_below_linux_single_argument_limit():
    script = evaluator_file_script(".trusted/requests/" + "a" * 40 + ".json")

    assert len(script.encode()) < 131_072
    assert max(len(line.encode()) for line in script.splitlines()) < 131_072


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
    assert "resolution=models_discriminated" in result.feedback
    assert calls == [result.critic_thread_id]
    request_path = (
        tmp_path
        / "run"
        / "workspace"
        / "critic-repository"
        / ".trusted"
        / "requests"
        / f"{git(workspace, 'log', '--format=%H', '--grep=actor theory and plan', '-1')}.json"
    )
    request = json.loads(request_path.read_text())
    assert request["source_path"] == "world_model.py"
    assert request["timeline_path"] == "canonical-input.json"
    assert "source" not in request
    assert "timeline" not in request
    report_path = (
        tmp_path
        / "run"
        / "workspace"
        / "critic-repository"
        / ".trusted"
        / "evaluations"
        / f"{git(workspace, 'log', '--format=%H', '--grep=actor theory and plan', '-1')}.json"
    )
    assert report_path.is_file()
    assert set(json.loads(report_path.read_text())["backtest"]["models"]) == {"a", "b"}
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


def test_dirty_repository_feedback_says_what_happened_and_how_to_fix_it(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    workspace = tmp_path / "run" / "workspace" / "innerContext"
    plan = experiment_plan()

    def edit(_call):
        write_plan(workspace, plan)
        (workspace / "uncommitted-notes.txt").write_text("scratch")

    physics, _actor = strategy(workspace, edit)
    result = physics.run(run_dir="run", max_cycles=1)

    assert result.accepted is False
    assert "evaluates only a clean committed HEAD" in result.feedback
    assert "No real action was attempted" in result.feedback
    assert "git status --short" in result.feedback
    assert "uncommitted-notes.txt" in result.feedback


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


def test_actor_system_prompt_is_detailed_and_domain_extensible():
    prompt = physics_actor_system_prompt("Color grids and ARC actions.")
    assert PHYSICS_ACTOR_SYSTEM_PROMPT == ACTOR_INSTRUCTIONS
    for required in (
        "Ground the state",
        "Discover mechanisms",
        "Infer utility",
        "canonical-input.json",
        "trusted-report.json",
        "step_<suffix>",
        "reward_<suffix>",
        "python backtest.py",
        "python plan.py",
        "python commit.py plan-N",
        "What happens after you answer",
        "wrong_prediction",
        "models_discriminated",
        "plan_exhausted",
        "max_actions",
        "Git, caching, and recovery",
    ):
        assert required in prompt
    assert "Color grids" in prompt


def test_actor_files_publish_the_same_complete_runbook(tmp_path):
    from eggopt import write_actor_files

    write_actor_files(tmp_path, ({"legal_actions": [1]},), "Toy domain details.")

    instructions = (tmp_path / "INSTRUCTIONS.md").read_text()
    assert instructions.startswith(ACTOR_INSTRUCTIONS)
    assert "Required procedure for every turn" in instructions
    assert "The trusted Critic operates on committed Git history" in instructions
    assert instructions.endswith("Toy domain details.\n")

    (tmp_path / "INSTRUCTIONS.md").write_text("obsolete generic instructions")
    write_actor_files(tmp_path, ({"legal_actions": [1]},), "Updated details.")
    refreshed = (tmp_path / "INSTRUCTIONS.md").read_text()
    assert refreshed.startswith(ACTOR_INSTRUCTIONS)
    assert refreshed.endswith("Updated details.\n")


def test_actor_turn_prompts_require_the_full_runbook():
    first = _actor_turn_prompt(1, {})
    revision = _actor_turn_prompt(2, {"feedback": "Prediction contradicted."})

    assert "complete runbook" in first
    assert "commit.py" in first
    assert "do not execute the real environment" in first
    assert "trusted-report.json" in revision
    assert "one new clean commit" in revision
    assert revision.endswith("Prediction contradicted.")


def test_critic_feedback_explains_each_execution_resolution():
    mismatch = _execution_feedback("wrong_prediction")
    assert "permanently appended" in mismatch
    assert "state grounding" in mismatch
    discriminated = _execution_feedback("models_discriminated")
    assert "compatible_models" in discriminated
    assert "first intent" in discriminated
    exhausted = _execution_feedback("plan_exhausted")
    assert "did not report the goal" in exhausted
    assert "reward/goal inference" in exhausted


def test_evaluation_report_path_requires_full_git_head():
    head = "a" * 40
    assert _evaluation_report_path(head) == f".trusted/evaluations/{head}.json"
    assert _evaluation_request_path(head) == f".trusted/requests/{head}.json"
    with pytest.raises(ValueError, match="full hexadecimal"):
        _evaluation_report_path("../escape")


def test_evaluation_report_rejects_incomplete_json(tmp_path):
    path = tmp_path / "report.json"
    path.write_text('{"backtest": {}}')
    with pytest.raises(TypeError, match="backtest or planning"):
        _evaluation_report(path)


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
