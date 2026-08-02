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
    RunnerConfig,
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
            execute=execute
            or (
                lambda timeline, **_: Value(
                    {"position": timeline[-1].get("next_state", timeline[-1])["position"] + 1,
                     "legal_actions": [1]}
                )
            ),
            is_goal=lambda state: state["position"] >= 2,
            identity={"domain": "toy"},
            domain_information="State has position and legal_actions.",
            max_depth=4,
            evaluator_timeout_sec=17,
        ),
        llm,
    )


def write_plan(workspace, plan, message="actor theory and plan"):
    (workspace / "world_model.py").write_text(MODEL)
    (workspace / "proposed-plans.json").write_text(json.dumps([plan]))
    (workspace / "committed-plan.json").write_text(json.dumps(plan))
    git(workspace, "add", "-A")
    git(workspace, "commit", "-m", message)


def experiment_plan():
    shared = {"position": 1, "legal_actions": [1]}
    return {
        "purpose": "experiment",
        "models": ["a", "b"],
        "intents": [
            {"action": 1, "prediction": {"a": shared, "b": shared}},
            {
                "action": 1,
                "prediction": {
                    "a": {"position": 2, "legal_actions": [1]},
                    "b": {"position": 3, "legal_actions": [1]},
                },
            },
        ],
    }


def run_evaluator(request):
    completed = subprocess.run(
        ["python", "-c", evaluator_script(request)],
        text=True,
        capture_output=True,
        check=True,
    )
    return parse_evaluator_output(completed.stdout)


def test_generic_evaluator_validates_actor_plan_with_structured_action():
    click = {"action": 6, "data": {"x": 12, "y": 34}}
    source = '''
from copy import deepcopy
def step_a(state, action):
    result = deepcopy(state)
    if action == {"action": 6, "data": {"x": 12, "y": 34}}:
        result["score"] += 1
    return result
def reward_a(state):
    return state["score"]
'''
    prediction = {"score": 1, "legal_actions": [click]}
    plan = {
        "purpose": "goal",
        "models": ["a"],
        "intents": [{"action": click, "prediction": {"a": prediction}}],
    }

    result = run_evaluator(
        {
            "source": source,
            "timeline": [{"score": 0, "legal_actions": [click]}],
            "plans": [plan],
            "legal_actions_key": "legal_actions",
            "max_depth": 2,
            "max_nodes": 20,
        }
    )

    assert result["planning"]["valid_plans"] == [
        {"plan_id": "plan-1", "plan": plan}
    ]
    assert result["planning"]["invalid_plans"] == []


def test_generic_evaluator_accepts_equivalent_structured_actions():
    click = {"action": 6, "data": {"x": 12, "y": 34}}
    source = '''
from copy import deepcopy
def actions_a(state):
    return [{"data": {"y": 34, "x": 12}, "action": 6}]
def step_a(state, action):
    result = deepcopy(state)
    result["clicked"] = True
    return result
def reward_a(state):
    return int(state.get("clicked", False))
'''
    prediction = {"clicked": True, "legal_actions": [6]}
    plan = {
        "purpose": "goal",
        "models": ["a"],
        "intents": [{"action": click, "prediction": {"a": prediction}}],
    }
    result = run_evaluator(
        {
            "source": source,
            "timeline": [{"clicked": False, "legal_actions": [6]}],
            "plans": [plan],
            "legal_actions_key": "legal_actions",
            "max_depth": 1,
            "max_nodes": 20,
        }
    )

    assert result["planning"]["valid_plans"][0]["plan"] == plan


def test_generic_evaluator_uses_model_specific_action_generators():
    click = {"action": 6, "data": {"x": 12, "y": 34}}
    source = '''
from copy import deepcopy
def _clicks(state):
    if 6 not in state["legal_actions"]:
        return []
    return [{"action": 6, "data": {"x": 12, "y": 34}}]
def actions_a(state):
    return _clicks(state)
def actions_b(state):
    return _clicks(state)
def step_a(state, action):
    result = deepcopy(state)
    result["branch"] = "a"
    return result
def reward_a(state):
    return 0
def step_b(state, action):
    result = deepcopy(state)
    result["branch"] = "b"
    return result
def reward_b(state):
    return 0
'''
    plan = {
        "purpose": "experiment",
        "models": ["a", "b"],
        "intents": [
            {
                "action": click,
                "prediction": {
                    "a": {"branch": "a", "legal_actions": [6]},
                    "b": {"branch": "b", "legal_actions": [6]},
                },
            }
        ],
    }
    result = run_evaluator(
        {
            "source": source,
            "timeline": [{"branch": None, "legal_actions": [6]}],
            "plans": [plan],
            "legal_actions_key": "legal_actions",
            "max_depth": 1,
            "max_nodes": 20,
        }
    )

    assert result["planning"]["valid_plans"][0]["plan"] == plan


def test_experiment_plan_requires_common_prefix_and_first_distinction():
    shared = {"position": 1, "legal_actions": [1]}
    divergent = {
        "a": {"position": 2, "legal_actions": [1]},
        "b": {"position": 3, "legal_actions": [1]},
    }
    valid = {
        "purpose": "experiment",
        "models": ["a", "b"],
        "intents": [
            {"action": 1, "prediction": {"a": shared, "b": shared}},
            {"action": 1, "prediction": divergent},
        ],
    }
    assert canonical_plan(valid) == valid

    early = json.loads(json.dumps(valid))
    early["intents"].append(early["intents"].pop(0))
    with pytest.raises(ValueError, match="common prefix"):
        canonical_plan(early)

    never = json.loads(json.dumps(valid))
    never["intents"][-1]["prediction"]["b"] = never["intents"][-1]["prediction"]["a"]
    with pytest.raises(ValueError, match="distinguishing action"):
        canonical_plan(never)


def test_evaluator_reports_invalid_actor_plans_without_searching():
    valid = experiment_plan()
    wrong_prediction = {
        "purpose": "goal",
        "models": ["a"],
        "intents": [
            {
                "action": 1,
                "prediction": {
                    "a": {"position": 99, "legal_actions": [1]}
                },
            }
        ],
    }
    illegal = json.loads(json.dumps(wrong_prediction))
    illegal["intents"][0]["action"] = 2
    too_long = json.loads(json.dumps(valid))

    result = run_evaluator(
        {
            "source": MODEL,
            "timeline": [{"position": 0, "legal_actions": [1]}],
            "plans": [valid, wrong_prediction, illegal, too_long],
            "legal_actions_key": "legal_actions",
            "max_depth": 1,
            "max_nodes": 20,
        }
    )

    assert result["planning"]["valid_plans"] == []
    errors = [item["error"] for item in result["planning"]["invalid_plans"]]
    assert "plan has 2 intents; limit is 1" in errors[0]
    assert "does not match step_a" in errors[1]
    assert "action is not legal" in errors[2]
    assert "plan has 2 intents; limit is 1" in errors[3]

    result = run_evaluator(
        {
            "source": MODEL,
            "timeline": [{"position": 0, "legal_actions": [1]}],
            "plans": [wrong_prediction, illegal],
            "legal_actions_key": "legal_actions",
            "max_depth": 4,
            "max_nodes": 20,
        }
    )
    errors = [item["error"] for item in result["planning"]["invalid_plans"]]
    assert "does not match step_a" in errors[0]
    assert "action is not legal" in errors[1]


def test_evaluator_bounds_total_submitted_plan_validation():
    plan = experiment_plan()
    with pytest.raises(subprocess.CalledProcessError):
        run_evaluator(
            {
                "source": MODEL,
                "timeline": [{"position": 0, "legal_actions": [1]}],
                "plans": [plan, plan],
                "legal_actions_key": "legal_actions",
                "max_depth": 4,
                "max_nodes": 3,
            }
        )


def test_generic_evaluator_rejects_orphan_action_generators():
    source = '''
def step_a(state, action):
    return state
def reward_a(state):
    return 0
def actions_missing(state):
    return []
'''
    with pytest.raises(subprocess.CalledProcessError):
        run_evaluator(
            {
                "source": source,
                "timeline": [{"legal_actions": [1]}],
                "legal_actions_key": "legal_actions",
                "max_depth": 1,
                "max_nodes": 20,
            }
        )


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
    (tmp_path / "proposed-plans.json").write_text(json.dumps([{
        "purpose": "goal",
        "models": ["a"],
        "intents": [{
            "action": 1,
            "prediction": {"a": {"position": 1, "legal_actions": [1]}},
        }],
    }]))
    request = tmp_path / "trusted" / "request.json"
    request.parent.mkdir()
    request.write_text(
        json.dumps(
            {
                "source_path": "world_model.py",
                "timeline_path": "canonical-input.json",
                "plans_path": "proposed-plans.json",
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

    assert len(script) < 20_000
    assert "x" * 1_000 not in script
    assert parse_evaluator_receipt(completed.stdout) == "trusted/report.json"
    assert set(json.loads(report.read_text())["backtest"]["models"]) == {"a", "b"}


def test_file_evaluator_script_stays_below_linux_single_argument_limit():
    script = evaluator_file_script(".trusted/requests/" + "a" * 40 + ".json")

    assert len(script.encode()) < 131_072
    assert max(len(line.encode()) for line in script.splitlines()) < 131_072


def test_generic_evaluator_validates_multistep_discrimination():
    shared = {"position": 1, "legal_actions": [1]}
    plan = {
        "purpose": "experiment",
        "models": ["a", "b"],
        "intents": [
            {"action": 1, "prediction": {"a": shared, "b": shared}},
            {
                "action": 1,
                "prediction": {
                    "a": {"position": 2, "legal_actions": [1]},
                    "b": {"position": 3, "legal_actions": [1]},
                },
            },
        ],
    }
    result = run_evaluator(
        {
            "source": MODEL,
            "timeline": [{"position": 0, "legal_actions": [1]}],
            "plans": [plan],
            "legal_actions_key": "legal_actions",
            "max_depth": 4,
            "max_nodes": 100,
        }
    )

    assert set(result["backtest"]["models"]) == {"a", "b"}
    assert result["planning"]["valid_plans"] == [
        {"plan_id": "plan-1", "plan": plan}
    ]


def test_evaluator_reports_models_even_when_none_survive():
    timeline = [
        {"position": 0, "legal_actions": [1]},
        {
            "state": {"position": 0, "legal_actions": [1]},
            "action": 1,
            "next_state": {"position": 99, "legal_actions": [1]},
        },
    ]
    result = run_evaluator(
        {
            "source": MODEL,
            "timeline": timeline,
            "plans": [],
            "legal_actions_key": "legal_actions",
            "max_depth": 4,
            "max_nodes": 100,
        }
    )

    assert result["backtest"]["surviving_models"] == []
    assert set(result["backtest"]["models"]) == {"a", "b"}
    assert result["planning"]["valid_plans"] == []


def test_physics_uses_critic_thread_python_exec_and_executes_until_branch(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    workspace = tmp_path / "run" / "workspace" / "innerContext"
    calls = []
    tools = ToolRegistry()

    def python_exec(arguments, context):
        calls.append((context.thread_id, context.timeout_sec))
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
        index = len(observed)
        next_state = plan["intents"][index]["prediction"]["a"]
        assert intent == plan["intents"][index]["action"]
        observed.append(next_state)
        return Value(next_state)

    physics, _actor = strategy(workspace, edit, tools=tools, execute=execute)
    result = physics.run(run_dir="run", max_cycles=1)

    assert result.accepted is False
    assert len(observed) == 2
    state = json.loads(
        (tmp_path / "run" / "workspace" / ".trusted" / "state.json").read_text()
    )
    assert [item["action"] for item in state["timeline"][1:]] == [1, 1]
    assert "resolution=models_discriminated" in result.feedback
    assert calls == [(result.critic_thread_id, 17)]
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


def test_critic_accepts_a_selected_nonfirst_actor_plan(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    workspace = tmp_path / "run" / "workspace" / "innerContext"
    selected = experiment_plan()
    invalid = json.loads(json.dumps(selected))
    invalid["intents"][0]["action"] = 2

    def edit(_call):
        (workspace / "world_model.py").write_text(MODEL)
        (workspace / "proposed-plans.json").write_text(
            json.dumps([invalid, selected])
        )
        (workspace / "committed-plan.json").write_text(json.dumps(selected))
        git(workspace, "add", "-A")
        git(workspace, "commit", "-m", "select second actor plan")

    observed = []

    def execute(intent, **_):
        index = len(observed)
        assert intent == selected["intents"][index]["action"]
        state = selected["intents"][index]["prediction"]["a"]
        observed.append(state)
        return Value(state)

    physics, _actor = strategy(workspace, edit, execute=execute)
    result = physics.run(run_dir="run", max_cycles=1)

    assert result.accepted is False
    assert "models_discriminated" in result.feedback
    assert len(observed) == 2
    planning = json.loads(
        (tmp_path / "run" / "workspace" / ".trusted" / "state.json").read_text()
    )["last_report"]["planning"]
    assert [item["plan_id"] for item in planning["invalid_plans"]] == ["plan-1"]
    assert [item["plan_id"] for item in planning["valid_plans"]] == ["plan-2"]


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
            (workspace / ".gitignore").write_text(
                "scratch/\ndirty.txt\n.physics-evaluation/\n"
            )
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
        "Infer the goal",
        "canonical-input.json",
        "trusted-report.json",
        "step_<suffix>",
        "proposed-plans.json",
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


def test_actor_instruments_are_self_contained_and_use_strategy_configuration(
    tmp_path,
):
    import os

    from eggopt import write_actor_files

    write_actor_files(
        tmp_path,
        ({"position": 0, "moves": [1]},),
        legal_actions_key="moves",
        max_depth=3,
        max_nodes=41,
        evaluator_timeout_sec=7,
    )
    (tmp_path / "world_model.py").write_text(
        "def step_a(state, action):\n"
        "    return {'position': state['position'] + action, 'moves': [1]}\n"
        "def reward_a(state):\n"
        "    return state['position']\n"
    )
    proposed = {
        "purpose": "goal",
        "models": ["a"],
        "intents": [
            {
                "action": 1,
                "prediction": {"a": {"position": 1, "moves": [1]}},
            },
            {
                "action": 1,
                "prediction": {"a": {"position": 2, "moves": [1]}},
            },
            {
                "action": 1,
                "prediction": {"a": {"position": 3, "moves": [1]}},
            },
        ],
    }
    (tmp_path / "proposed-plans.json").write_text(json.dumps([proposed]))

    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    for script in ("backtest.py", "plan.py"):
        completed = subprocess.run(
            ["python", "-E", script],
            cwd=tmp_path,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr

    config = json.loads((tmp_path / "physics-config.json").read_text())
    assert config == {
        "evaluator_timeout_sec": 7,
        "legal_actions_key": "moves",
        "max_depth": 3,
        "max_nodes": 41,
    }
    assert json.loads((tmp_path / "backtest-report.json").read_text())[
        "surviving_models"
    ] == ["a"]
    plans = json.loads((tmp_path / "plan-report.json").read_text())["valid_plans"]
    assert plans and len(plans[0]["plan"]["intents"]) == 3
    runtime = (tmp_path / "physics_runtime.py").read_text()
    assert "eggopt" not in runtime
    assert "arcagi3" not in runtime


def test_actor_instruments_run_in_a_clean_python_container(tmp_path):
    import os
    import shutil

    if shutil.which("docker") is None:
        pytest.skip("Docker is unavailable")
    from eggopt import write_actor_files

    write_actor_files(
        tmp_path,
        ({"position": 0, "legal_actions": [1]},),
        max_depth=2,
        max_nodes=20,
    )
    (tmp_path / "world_model.py").write_text(
        "def step_a(state, action):\n"
        "    return {'position': state['position'] + action, 'legal_actions': [1]}\n"
        "def reward_a(state):\n"
        "    return state['position']\n"
    )
    proposal = {
        "purpose": "goal",
        "models": ["a"],
        "intents": [
            {
                "action": 1,
                "prediction": {
                    "a": {"position": 1, "legal_actions": [1]}
                },
            }
        ],
    }
    (tmp_path / "proposed-plans.json").write_text(json.dumps([proposal]))
    completed = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--cap-drop",
            "ALL",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "-e",
            "HOME=/tmp",
            "-v",
            f"{tmp_path}:/workspace",
            "-w",
            "/workspace",
            "python:3.12-slim",
            "python",
            "plan.py",
        ],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads((tmp_path / "plan-report.json").read_text())[
        "valid_plans"
    ]


def test_local_and_trusted_plan_validation_match(tmp_path):
    import importlib.util

    from eggopt import write_actor_files

    write_actor_files(tmp_path, ({"legal_actions": [1]},))
    spec = importlib.util.spec_from_file_location(
        "generated_physics_runtime", tmp_path / "physics_runtime.py"
    )
    assert spec is not None and spec.loader is not None
    runtime = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runtime)
    values = (
        {"purpose": "goal", "models": ["a"], "intents": []},
        {
            "purpose": "goal",
            "models": ["a"],
            "intents": [{"action": 1, "prediction": {"b": {}}}],
        },
        {
            "purpose": "goal",
            "models": ["a"],
            "intents": [{"action": 1, "prediction": {"a": {}}}],
            "extra": True,
        },
    )
    for value in values:
        with pytest.raises(ValueError) as trusted:
            canonical_plan(value)
        with pytest.raises(ValueError) as local:
            runtime.canonical_plan(value)
        assert str(local.value) == str(trusted.value)


def test_commit_rejects_a_stale_plan_report(tmp_path, monkeypatch):
    import importlib.util

    from eggopt import write_actor_files

    monkeypatch.chdir(tmp_path)
    write_actor_files(tmp_path, ({"legal_actions": [1]},))
    proposal = {
        "purpose": "goal",
        "models": ["a"],
        "intents": [
            {
                "action": 1,
                "prediction": {"a": {"legal_actions": [1]}},
            }
        ],
    }
    (tmp_path / "proposed-plans.json").write_text(json.dumps([]))
    (tmp_path / "plan-report.json").write_text(
        json.dumps({"valid_plans": [{"plan_id": "plan-1", "plan": proposal}]})
    )
    spec = importlib.util.spec_from_file_location(
        "generated_physics_runtime_stale", tmp_path / "physics_runtime.py"
    )
    assert spec is not None and spec.loader is not None
    runtime = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runtime)

    with pytest.raises(SystemExit, match="stale"):
        runtime.actor_commit("plan-1")


def test_actor_instrument_subprocess_has_a_timeout(tmp_path):
    import os

    from eggopt import write_actor_files

    write_actor_files(
        tmp_path,
        ({"legal_actions": [1]},),
        evaluator_timeout_sec=0.05,
    )
    (tmp_path / "world_model.py").write_text(
        "while True:\n"
        "    pass\n"
        "def step_a(state, action):\n"
        "    return state\n"
        "def reward_a(state):\n"
        "    return 0\n"
    )
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)

    completed = subprocess.run(
        ["python", "-E", "backtest.py"],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        timeout=2,
        check=False,
    )

    assert completed.returncode != 0
    assert "timed out after 0.05 seconds" in completed.stderr


def test_actor_instrument_timeout_terminates_descendants(tmp_path):
    import os
    import time

    from eggopt import write_actor_files

    write_actor_files(
        tmp_path,
        ({"legal_actions": [1]},),
        evaluator_timeout_sec=0.2,
    )
    marker = tmp_path / "descendant-survived"
    (tmp_path / "world_model.py").write_text(
        "import subprocess, sys\n"
        f"subprocess.Popen([sys.executable, '-c', "
        f"\"import time; time.sleep(0.8); open({str(marker)!r}, 'w').write('bad')\"])\n"
        "while True:\n"
        "    pass\n"
    )
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)

    completed = subprocess.run(
        ["python", "-E", "backtest.py"],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        timeout=2,
        check=False,
    )
    time.sleep(0.9)

    assert completed.returncode != 0
    assert "timed out after 0.2 seconds" in completed.stderr
    assert not marker.exists()


def test_existing_repository_refreshes_only_owned_instruments(tmp_path):
    from eggopt.physics.strategy import _refresh_actor_instruments

    actor = tmp_path / "actor"
    critic = tmp_path / "critic"
    actor.mkdir()
    git(actor, "init", "-b", "main")
    git(actor, "config", "user.name", "Physics")
    git(actor, "config", "user.email", "physics@test")
    (actor / "world_model.py").write_text("THEORY = 'preserve me'\n")
    (actor / "backtest.py").write_text(
        'from eggopt.physics import actor_backtest\n\n'
        'if __name__ == "__main__":\n'
        '    actor_backtest()\n'
    )
    git(actor, "add", "-A")
    git(actor, "commit", "-m", "old instruments")
    subprocess.run(
        ["git", "clone", "--no-local", str(actor), str(critic)],
        check=True,
        text=True,
        capture_output=True,
    )

    _refresh_actor_instruments(
        actor,
        critic,
        domain_information="Updated domain contract.",
        legal_actions_key="moves",
        max_depth=5,
        max_nodes=99,
        evaluator_timeout_sec=11,
    )

    assert (actor / "world_model.py").read_text() == "THEORY = 'preserve me'\n"
    assert "physics_runtime" in (actor / "backtest.py").read_text()
    assert (actor / "INSTRUCTIONS.md").read_text().endswith(
        "Updated domain contract.\n"
    )
    assert json.loads((actor / "physics-config.json").read_text())["max_depth"] == 5
    assert git(actor, "log", "-1", "--format=%s") == (
        "[physics] refresh Actor instruments"
    )
    assert git(actor, "rev-parse", "HEAD") == git(critic, "rev-parse", "HEAD")


def test_instrument_refresh_refuses_modified_owned_files(tmp_path):
    from eggopt.physics.strategy import _refresh_actor_instruments

    actor = tmp_path / "actor"
    critic = tmp_path / "critic"
    actor.mkdir()
    git(actor, "init", "-b", "main")
    git(actor, "config", "user.name", "Physics")
    git(actor, "config", "user.email", "physics@test")
    (actor / "backtest.py").write_text("old\n")
    git(actor, "add", "-A")
    git(actor, "commit", "-m", "old instruments")
    subprocess.run(
        ["git", "clone", "--no-local", str(actor), str(critic)],
        check=True,
        text=True,
        capture_output=True,
    )
    (actor / "backtest.py").write_text("actor modification\n")

    with pytest.raises(RuntimeError, match="modified Physics-owned.*backtest.py"):
        _refresh_actor_instruments(
            actor,
            critic,
            domain_information="",
            legal_actions_key="legal_actions",
            max_depth=8,
            max_nodes=10_000,
            evaluator_timeout_sec=300,
        )

    assert (actor / "backtest.py").read_text() == "actor modification\n"


def test_instrument_refresh_refuses_committed_custom_helpers(tmp_path):
    from eggopt.physics.strategy import _refresh_actor_instruments

    actor = tmp_path / "actor"
    critic = tmp_path / "critic"
    actor.mkdir()
    git(actor, "init", "-b", "main")
    git(actor, "config", "user.name", "Physics")
    git(actor, "config", "user.email", "physics@test")
    (actor / "backtest.py").write_text("custom committed helper\n")
    git(actor, "add", "-A")
    git(actor, "commit", "-m", "custom helper")
    subprocess.run(
        ["git", "clone", "--no-local", str(actor), str(critic)],
        check=True,
        text=True,
        capture_output=True,
    )

    with pytest.raises(RuntimeError, match="customized.*backtest.py"):
        _refresh_actor_instruments(
            actor,
            critic,
            domain_information="",
            legal_actions_key="legal_actions",
            max_depth=8,
            max_nodes=10_000,
            evaluator_timeout_sec=300,
        )

    assert (actor / "backtest.py").read_text() == "custom committed helper\n"


def test_git_critic_does_not_evaluate_an_instrument_maintenance_commit(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    workspace = tmp_path / "run" / "workspace" / "innerContext"
    calls = []

    def edit(call):
        calls.append(call)
        if call == 1:
            (workspace / "backtest.py").write_text(
                'from eggopt.physics import actor_backtest\n\n'
                'if __name__ == "__main__":\n'
                '    actor_backtest()\n'
            )
            git(workspace, "add", "backtest.py")
            git(workspace, "commit", "-m", "restore legacy helper")
        else:
            write_plan(workspace, experiment_plan())

    physics, actor = strategy(workspace, edit, replies=("legacy", "ready"))
    result = physics.run(run_dir="run", max_cycles=2)

    assert actor.calls == 2
    assert calls == [1, 2]
    assert "models_discriminated" in result.feedback
    subjects = git(workspace, "log", "--format=%s", "-3").splitlines()
    assert "[physics] refresh Actor instruments" in subjects


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
    assert "goal inference" in exhausted


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


def test_physics_task_reuses_an_existing_runtime_thread(tmp_path, monkeypatch):
    import asyncio

    from eggopt.runtime import Runtime

    from eggthreads import create_child_thread, create_root_thread

    monkeypatch.chdir(tmp_path)
    workspace = (
        tmp_path / "benchmark" / "environments" / "toy" / "workspace" / "innerContext"
    )
    plan = canonical_plan(
        {
            "purpose": "goal",
            "models": ["a"],
            "intents": [
                {
                    "action": 1,
                    "prediction": {"a": {"position": 2, "legal_actions": [1]}},
                }
            ],
        }
    )

    def edit(_call):
        write_plan(workspace, plan)

    physics, _actor = strategy(workspace, edit)
    with Runtime.open("benchmark") as runtime:
        root = create_root_thread(runtime.threads, name="Benchmark")
        physics_id = create_child_thread(runtime.threads, root, name="Physics toy")
        result = asyncio.run(
            runtime.flow.run(
                physics.task(
                    runtime_key=runtime.runtime_key,
                    run_dir=tmp_path / "benchmark" / "environments" / "toy",
                    physics_thread_id=physics_id,
                    max_cycles=1,
                )
            )
        )

        assert result.physics_thread_id == physics_id
        assert list_root_threads(runtime.threads) == [root]
        assert [
            name for _, name, *_ in list_children_with_meta(runtime.threads, root)
        ] == ["Physics toy"]


def test_scheduler_managed_agent_is_part_of_task_identity():
    ordinary = Agent(object(), {"role": "actor"})
    managed = Agent(object(), {"role": "actor"}, scheduler_managed=True)

    assert "scheduler_managed" not in ordinary.task_identity
    assert managed.task_identity["scheduler_managed"] is True
    assert ordinary.task_identity != managed.task_identity
    assert managed.runner_config == RunnerConfig()


def test_scheduler_managed_turn_waits_for_shared_scheduler(tmp_path, monkeypatch):
    import asyncio

    from eggopt.actor_critic import _AgentTurn
    from eggopt.context import _bind_evaluation_runtime

    from eggthreads import (
        append_message,
        create_root_thread,
        load_thread_projection,
    )

    db = ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    thread_id = create_root_thread(db, name="Actor")
    agent = Agent(object(), {"role": "actor"}, scheduler_managed=True)
    task = _AgentTurn("runtime", thread_id, agent, "go", "actor", 1)
    _bind_evaluation_runtime("runtime", db)

    async def answer_after_scheduler_started():
        while not any(
            message.payload.get("role") == "user"
            for message in load_thread_projection(db, thread_id).messages
        ):
            await asyncio.sleep(0)
        append_message(db, thread_id, "assistant", "done")

    async def exercise():
        responder = asyncio.create_task(answer_after_scheduler_started())
        try:
            return await task.run()
        finally:
            await responder

    try:
        assert asyncio.run(exercise()) == "done"
    finally:
        db.close()


def test_scheduler_managed_wait_uses_bounded_token_accounting(tmp_path, monkeypatch):
    import asyncio

    from eggopt.actor_critic import _wait_until_waiting

    from eggthreads import append_message, create_root_thread

    db = ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    thread_id = create_root_thread(db, name="Actor")
    prompt_id = append_message(db, thread_id, "user", "go")
    after_seq = db.max_event_seq(thread_id)
    calls = []

    def bounded_stats(_db, observed_thread_id):
        calls.append(observed_thread_id)
        return {"full_thread_tokens": 1}

    def forbidden_full_stats(*_args, **_kwargs):
        raise AssertionError("scheduler wait must not rebuild full token statistics")

    monkeypatch.setattr("eggthreads.header_token_stats", bounded_stats)
    monkeypatch.setattr("eggthreads.thread_token_stats", forbidden_full_stats)

    async def answer():
        await asyncio.sleep(0)
        append_message(db, thread_id, "assistant", "done")

    async def exercise():
        responder = asyncio.create_task(answer())
        try:
            await _wait_until_waiting(
                db,
                thread_id,
                prompt_id,
                after_seq,
                100,
            )
        finally:
            await responder

    try:
        asyncio.run(exercise())
        assert calls
    finally:
        db.close()


def test_scheduler_managed_wait_confirms_a_bounded_limit_exactly(
    tmp_path, monkeypatch
):
    import asyncio

    from eggopt.actor_critic import _wait_until_waiting

    from eggflow import ContextLimitExceededError
    from eggthreads import append_message, create_root_thread

    db = ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    thread_id = create_root_thread(db, name="Actor")
    prompt_id = append_message(db, thread_id, "user", "go")
    after_seq = db.max_event_seq(thread_id)
    exact_calls = []

    monkeypatch.setattr(
        "eggthreads.header_token_stats",
        lambda _db, _thread_id: {"full_thread_tokens": 100},
    )

    def exact(_db, observed_thread_id):
        exact_calls.append(observed_thread_id)
        return 100

    monkeypatch.setattr("eggopt.context_limit.full_context_tokens", exact)

    try:
        with pytest.raises(ContextLimitExceededError):
            asyncio.run(
                _wait_until_waiting(
                    db,
                    thread_id,
                    prompt_id,
                    after_seq,
                    100,
                )
            )
        assert exact_calls == [thread_id]
    finally:
        db.close()


def test_scheduler_managed_wait_does_not_project_answers_while_streaming(
    tmp_path, monkeypatch
):
    import asyncio

    from eggopt.actor_critic import _wait_until_waiting

    from eggthreads import append_message, create_root_thread

    db = ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    thread_id = create_root_thread(db, name="Actor")
    prompt_id = append_message(db, thread_id, "user", "go")
    after_seq = db.max_event_seq(thread_id)
    states = iter(("running", "waiting_user"))
    projected = []

    def state(_db, observed_thread_id):
        assert observed_thread_id == thread_id
        return next(states)

    def answer(_db, observed_thread_id, observed_after_seq):
        projected.append((observed_thread_id, observed_after_seq))
        return "done"

    monkeypatch.setattr("eggopt.actor_critic.thread_state", state)
    monkeypatch.setattr("eggopt.actor_critic._latest_answer", answer)

    async def append_activity():
        await asyncio.sleep(0.1)
        append_message(db, thread_id, "assistant", "partial activity")

    async def exercise():
        activity = asyncio.create_task(append_activity())
        try:
            await _wait_until_waiting(
                db,
                thread_id,
                prompt_id,
                after_seq,
                None,
            )
        finally:
            await activity

    try:
        asyncio.run(exercise())
        assert projected == [(thread_id, after_seq)]
    finally:
        db.close()


def test_scheduler_managed_wait_only_reduces_state_after_new_events(
    tmp_path, monkeypatch
):
    import asyncio

    from eggopt.actor_critic import _wait_until_waiting

    from eggthreads import append_message, create_root_thread

    db = ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    thread_id = create_root_thread(db, name="Actor")
    prompt_id = append_message(db, thread_id, "user", "go")
    after_seq = db.max_event_seq(thread_id)
    calls = []

    def state(_db, observed_thread_id):
        calls.append(observed_thread_id)
        return "running"

    monkeypatch.setattr("eggopt.actor_critic.thread_state", state)

    async def change_state():
        await asyncio.sleep(0.35)
        append_message(db, thread_id, "assistant", "done")

    async def exercise():
        changer = asyncio.create_task(change_state())
        try:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(
                    _wait_until_waiting(
                        db,
                        thread_id,
                        prompt_id,
                        after_seq,
                        None,
                    ),
                    timeout=0.55,
                )
        finally:
            await changer

    try:
        asyncio.run(exercise())
        assert calls == [thread_id, thread_id]
    finally:
        db.close()
