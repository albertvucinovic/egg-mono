from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

import pytest
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
    TerminalOutcome,
    canonical_plan,
    physics_actor_system_prompt,
    write_actor_files,
)
from eggthreads import RunnerConfig, ThreadsDB, ToolRegistry

MODEL = """
def step_a(state, action):
    return {"position": state["position"] + action["action"], "legal_actions": [1]}
def reward_a(state):
    return state["position"]
def step_b(state, action):
    amount = 1 if state["position"] == 0 else 2
    return {"position": state["position"] + amount, "legal_actions": [1]}
def reward_b(state):
    return state["position"]
"""


class ScriptedLLM:
    current_model_key = "***"

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


def run_evaluator(request):
    completed = subprocess.run(
        ["python", "-c", evaluator_script(request)],
        text=True,
        capture_output=True,
        check=True,
    )
    return parse_evaluator_output(completed.stdout)


def state(position):
    return {"position": position, "legal_actions": [1]}


def trajectory(*positions):
    return [
        {"state": state(left), "action": {"action": 1}, "next_state": state(right)}
        for left, right in pairwise(positions)
    ]


def strategy(
    workspace,
    edit,
    *,
    replies=("ready",),
    tools=None,
    execute=None,
    terminal_outcome=None,
):
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
            observe=lambda **_: Value(state(0)),
            execute=execute
            or (
                lambda timeline, action, **_: Value(
                    state(
                        timeline[-1].get("next_state", timeline[-1])["position"]
                        + action["action"]
                    )
                )
            ),
            validate_action=lambda state, action: (
                None
                if action == {"action": 1} and 1 in state["legal_actions"]
                else (_ for _ in ()).throw(ValueError("illegal toy action"))
            ),
            is_goal=lambda value: value["position"] == 2,
            identity={"domain": "toy"},
            terminal_outcome=terminal_outcome,
            domain_information="State has position and legal_actions.",
            planner_actions=({"action": 1},),
            max_depth=4,
            evaluator_timeout_sec=17,
        ),
        llm,
    )


def write_plan(workspace, plan, message="actor theory and plan"):
    (workspace / "world_model.py").write_text(MODEL)
    (workspace / "plan.json").write_text(json.dumps(plan))
    git(workspace, "add", "-A")
    git(workspace, "commit", "-m", message)


def test_plan_is_one_untyped_continuous_trajectory():
    plan = trajectory(0, 1, 2)
    assert canonical_plan(plan) == plan
    assert all(set(item) == {"state", "action", "next_state"} for item in plan)
    with pytest.raises(ValueError, match="non-empty"):
        canonical_plan([])
    broken = json.loads(json.dumps(plan))
    broken[1]["state"] = state(9)
    with pytest.raises(ValueError, match="continuous"):
        canonical_plan(broken)
    with pytest.raises(ValueError, match="exactly state"):
        canonical_plan([{"state": state(0), "action": {"action": 1}}])


def test_evaluator_validates_actor_trajectory_without_planner_rediscovery():
    plan = trajectory(0, 1, 2)
    result = run_evaluator(
        {
            "source": MODEL,
            "timeline": [state(0)],
            "plan": plan,
            "planner_actions": [],
            "max_depth": 4,
            "max_nodes": 100,
        }
    )
    validation = result["plan_validation"]
    assert validation["valid"] is True
    assert validation["supporting_models"] == ["a"]
    assert validation["plan"] == plan
    assert result["planning"]["suggestions"] == []


def test_evaluator_rejects_wrong_or_discontinuous_trajectory():
    wrong = trajectory(0, 99)
    result = run_evaluator(
        {
            "source": MODEL,
            "timeline": [state(0)],
            "plan": wrong,
            "planner_actions": [{"action": 1}],
            "max_depth": 4,
            "max_nodes": 100,
        }
    )
    assert result["plan_validation"]["valid"] is False
    assert "no Timeline-consistent" in result["plan_validation"]["error"]


def test_optional_rewards_enable_advisory_planning():
    result = run_evaluator(
        {
            "source": MODEL,
            "timeline": [state(0)],
            "plan": trajectory(0, 1),
            "planner_actions": [{"action": 1}],
            "max_depth": 4,
            "max_nodes": 100,
        }
    )
    planning = result["planning"]
    assert planning["eligible_models"] == ["a", "b"]
    assert any(item["kind"] == "reward" for item in planning["suggestions"])
    distinction = next(
        item for item in planning["suggestions"] if item["kind"] == "distinction"
    )
    assert len(distinction["plan"]) == 2
    final = distinction["plan"][-1]
    assert final == {
        "state": state(1),
        "action": {"action": 1},
        "next_state": state(2),
    }


def test_step_only_models_enable_pairwise_distinction_search():
    source = """
def step_a(state, action):
    return {"position": state["position"] + 1, "legal_actions": [1]}
def step_b(state, action):
    amount = 1 if state["position"] == 0 else 2
    return {"position": state["position"] + amount, "legal_actions": [1]}
"""
    result = run_evaluator(
        {
            "source": source,
            "timeline": [state(0)],
            "plan": trajectory(0, 1),
            "planner_actions": [{"action": 1}],
            "max_depth": 2,
            "max_nodes": 20,
        }
    )

    planning = result["planning"]
    assert planning["eligible_models"] == ["a", "b"]
    assert not any(item["kind"] == "reward" for item in planning["suggestions"])
    distinction = next(
        item for item in planning["suggestions"] if item["kind"] == "distinction"
    )
    assert distinction["models"] == ["a", "b"]
    assert distinction["plan"] == trajectory(0, 1, 2)


def test_reward_planner_maximizes_reward_within_search_bounds():
    source = """
def step_climb(state, action):
    return {"position": state["position"] + action["action"], "legal_actions": [1]}
def reward_climb(state):
    return state["position"]
"""
    result = run_evaluator(
        {
            "source": source,
            "timeline": [state(0)],
            "plan": trajectory(0, 1),
            "planner_actions": [{"action": 1}],
            "max_depth": 4,
            "max_nodes": 100,
        }
    )

    suggestion = next(
        item
        for item in result["planning"]["suggestions"]
        if item["kind"] == "reward" and item["models"] == ["climb"]
    )
    assert suggestion["plan"] == trajectory(0, 1, 2, 3, 4)


def test_reward_planner_keeps_shortest_first_path_to_equal_maximum():
    source = """
def step_peak(state, action):
    return {"position": state["position"] + action["action"], "legal_actions": [1, 2]}
def reward_peak(state):
    return min(state["position"], 2)
"""
    result = run_evaluator(
        {
            "source": source,
            "timeline": [
                {"position": 0, "legal_actions": [1, 2]},
            ],
            "plan": [
                {
                    "state": {"position": 0, "legal_actions": [1, 2]},
                    "action": {"action": 1},
                    "next_state": {"position": 1, "legal_actions": [1, 2]},
                }
            ],
            "planner_actions": [{"action": 1}, {"action": 2}],
            "max_depth": 3,
            "max_nodes": 100,
        }
    )

    suggestion = next(
        item
        for item in result["planning"]["suggestions"]
        if item["kind"] == "reward" and item["models"] == ["peak"]
    )
    assert [item["action"] for item in suggestion["plan"]] == [{"action": 2}]


def test_astar_planner_uses_goal_and_heuristic():
    source = """
def step_main(state, action):
    return {"position": state["position"] + action["action"], "legal_actions": [1, 2]}
def goal_main(state):
    return state["position"] >= 4
def heuristic_main(state):
    return max(0, 4 - state["position"]) / 2
def subgoal_main(state):
    return "reach four" if state["position"] < 4 else "complete"
"""
    initial = {"position": 0, "legal_actions": [1, 2]}
    result = run_evaluator(
        {
            "source": source,
            "timeline": [initial],
            "plan": [
                {
                    "state": initial,
                    "action": {"action": 1},
                    "next_state": {"position": 1, "legal_actions": [1, 2]},
                }
            ],
            "planner_actions": [{"action": 1}, {"action": 2}],
            "max_depth": 4,
            "max_nodes": 100,
            "search": "astar",
        }
    )

    suggestion = next(
        item for item in result["planning"]["suggestions"] if item["kind"] == "goal"
    )
    assert [item["action"] for item in suggestion["plan"]] == [
        {"action": 2},
        {"action": 2},
    ]
    assert suggestion["search"]["outcome"] == "goal_found"
    assert suggestion["search"]["optimality"] == "not_proven_actor_heuristic"
    assert suggestion["subgoals"] == [
        {"depth": 0, "label": "reach four"},
        {"depth": 2, "label": "complete"},
    ]


def test_auto_falls_back_to_reward_when_astar_hits_depth_limit():
    source = """
def step_main(state, action):
    return {"position": state["position"] + 1, "legal_actions": [1]}
def reward_main(state):
    return state["position"]
def goal_main(state):
    return state["position"] >= 5
def heuristic_main(state):
    return max(0, 5 - state["position"])
"""
    result = run_evaluator(
        {
            "source": source,
            "timeline": [state(0)],
            "plan": trajectory(0, 1),
            "planner_actions": [{"action": 1}],
            "max_depth": 2,
            "max_nodes": 100,
            "search": "auto",
        }
    )

    assert any(
        search["kind"] == "astar" and search["outcome"] == "depth_limit"
        for search in result["planning"]["searches"]
    )
    reward = next(
        item for item in result["planning"]["suggestions"] if item["kind"] == "reward"
    )
    assert reward["plan"] == trajectory(0, 1, 2)


def test_astar_without_heuristic_proves_shortest_unit_cost_path():
    source = """
def step_main(state, action):
    return {"position": state["position"] + action["action"], "legal_actions": [1, 2]}
def goal_main(state):
    return state["position"] == 2
"""
    initial = {"position": 0, "legal_actions": [1, 2]}
    result = run_evaluator(
        {
            "source": source,
            "timeline": [initial],
            "plan": [
                {
                    "state": initial,
                    "action": {"action": 1},
                    "next_state": {"position": 1, "legal_actions": [1, 2]},
                }
            ],
            "planner_actions": [{"action": 1}, {"action": 2}],
            "max_depth": 3,
            "max_nodes": 100,
            "search": "astar",
        }
    )
    suggestion = next(
        item for item in result["planning"]["suggestions"] if item["kind"] == "goal"
    )

    assert [item["action"] for item in suggestion["plan"]] == [{"action": 2}]
    assert suggestion["search"]["optimality"] == "proven_shortest"


def test_step_without_reward_still_backtests_and_validates():
    source = """
def step_a(state, action):
    return {"position": state["position"] + 1, "legal_actions": [1]}
"""
    result = run_evaluator(
        {
            "source": source,
            "timeline": [state(0)],
            "plan": trajectory(0, 1),
            "planner_actions": [{"action": 1}],
            "max_depth": 2,
            "max_nodes": 20,
        }
    )
    assert result["plan_validation"]["valid"] is True
    assert result["planning"]["eligible_models"] == ["a"]
    assert result["planning"]["suggestions"] == []


def test_evaluator_backtests_raw_timeline_actions():
    timeline = [
        state(0),
        {"state": state(0), "action": {"action": 1}, "next_state": state(1)},
    ]
    result = run_evaluator(
        {
            "source": MODEL,
            "timeline": timeline,
            "plan": [{"state": state(1), "action": {"action": 1}, "next_state": state(2)}],
            "planner_actions": [],
            "max_depth": 2,
            "max_nodes": 20,
        }
    )
    assert result["backtest"]["models"]["a"]["matches"] == 1


def test_generic_evaluator_rejects_orphan_rewards():
    source = """
def step_a(state, action):
    return state
def reward_missing(state):
    return 0
"""
    with pytest.raises(subprocess.CalledProcessError):
        run_evaluator(
            {
                "source": source,
                "timeline": [state(0)],
                "plan": trajectory(0, 1),
                "planner_actions": [],
                "max_depth": 1,
                "max_nodes": 20,
            }
        )


def test_generic_evaluator_can_write_compact_receipt(tmp_path):
    report = tmp_path / "trusted" / "report.json"
    request = {
        "source": MODEL,
        "timeline": [state(0)],
        "plan": trajectory(0, 1),
        "planner_actions": [],
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
    assert json.loads(report.read_text())["plan_validation"]["valid"] is True


def test_file_evaluator_loads_large_inputs(tmp_path):
    (tmp_path / "canonical-input.json").write_text(
        json.dumps({"timeline": [{**state(0), "irrelevant": "x" * 200_000}]})
    )
    # Model preserves the extra public field for the submitted first state.
    source = """
def step_a(state, action):
    result = dict(state)
    result["position"] += 1
    return result
"""
    (tmp_path / "world_model.py").write_text(source)
    current = {**state(0), "irrelevant": "x" * 200_000}
    predicted = {**current, "position": 1}
    (tmp_path / "plan.json").write_text(
        json.dumps([{"state": current, "action": {"action": 1}, "next_state": predicted}])
    )
    request = tmp_path / "trusted" / "request.json"
    request.parent.mkdir()
    request.write_text(
        json.dumps(
            {
                "source_path": "world_model.py",
                "timeline_path": "canonical-input.json",
                "plan_path": "plan.json",
                "planner_actions": [],
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
    assert len(script.encode()) < 131_072
    assert "x" * 1_000 not in script
    assert parse_evaluator_receipt(completed.stdout) == "trusted/report.json"


def test_physics_executes_raw_actions_and_reports_alternative_model(
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

    tools.register("bash", "unused", {"type": "object", "properties": {}}, lambda _: "")
    tools.register(
        "python_exec",
        "sandbox evaluator",
        {"type": "object", "properties": {"script": {"type": "string"}}},
        python_exec,
        accepts_context=True,
    )
    plan = trajectory(0, 1, 2)

    def edit(_call):
        write_plan(workspace, plan)

    observed_actions = []

    def execute(action, timeline, **_):
        observed_actions.append(action)
        # First transition matches both; second follows model b and contradicts plan a.
        position = 1 if len(observed_actions) == 1 else 3
        return Value(state(position))

    physics, _actor = strategy(workspace, edit, tools=tools, execute=execute)
    result = physics.run(run_dir="run", max_cycles=1)

    assert result.accepted is False
    assert observed_actions == [{"action": 1}, {"action": 1}]
    assert "wrong_prediction" in result.feedback
    assert "['b']" in result.feedback
    authoritative = json.loads(
        (tmp_path / "run" / "workspace" / ".trusted" / "state.json").read_text()
    )
    report = authoritative["last_report"]
    assert report["matching_models"] == ["b"]
    assert [item["action"] for item in report["executed"]] == observed_actions
    actor_timeline = json.loads((workspace / "canonical-input.json").read_text())[
        "timeline"
    ]
    actor_report = json.loads((workspace / "trusted-report.json").read_text())
    assert actor_timeline == authoritative["timeline"]
    assert actor_report == report
    critic = tmp_path / "run" / "workspace" / "critic-repository"
    assert git(critic, "status", "--short") == ""
    committed = set(
        git(critic, "show", "--format=", "--name-only", "HEAD").splitlines()
    )
    assert {
        ".trusted/state.json",
        "canonical-input.json",
        "trusted-report.json",
    } <= committed
    assert calls == [(result.critic_thread_id, 17)]
    from eggthreads import get_thread_sandbox_config, get_thread_working_directory

    db = ThreadsDB(tmp_path / "run" / ".egg" / "threads.sqlite")
    try:
        actor_dir = get_thread_working_directory(db, result.actor_thread_id)
        critic_dir = get_thread_working_directory(db, result.critic_thread_id)
        assert actor_dir == workspace
        assert critic_dir == tmp_path / "run" / "workspace" / "critic-repository"
        assert get_thread_sandbox_config(db, result.actor_thread_id).enabled
        assert get_thread_sandbox_config(db, result.critic_thread_id).enabled
    finally:
        db.close()


def test_domain_action_validation_happens_before_execution(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    workspace = tmp_path / "run" / "workspace" / "innerContext"
    plan = [{"state": state(0), "action": {"action": 2}, "next_state": state(2)}]

    def edit(_call):
        write_plan(workspace, plan)

    executed = []
    physics, _actor = strategy(
        workspace,
        edit,
        execute=lambda **_: executed.append(True) or Value(state(2)),
    )
    result = physics.run(run_dir="run", max_cycles=1)
    assert result.accepted is False
    assert executed == []
    assert "illegal toy action" in result.feedback


def test_dirty_repository_rejected_then_fixed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    workspace = tmp_path / "run" / "workspace" / "innerContext"
    plan = trajectory(0, 1, 2)

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


def test_actor_prompt_explains_freedom_and_minimal_interface():
    prompt = physics_actor_system_prompt("Domain-defined action objects.")
    assert PHYSICS_ACTOR_SYSTEM_PROMPT == ACTOR_INSTRUCTIONS
    for required in (
        "You own this Git repository",
        "answer_user_while_preserving_llm_turn",
        "visible running lab notebook",
        "world-grounding observations",
        "surprising observations",
        "contradictions",
        "notes to your future self",
        "does **not** end the current LLM turn",
        "plain assistant answer",
        "world_model.py",
        "matching `reward_<suffix>",
        "plan.json",
        "{state, action, next_state}",
        "hypothesis you consider most likely",
        "continue beyond the first action",
        "planner compares every pair",
        "does not require matching `reward_*`",
        "normal first attempt",
        "read the docstring at the top of `plan.py`",
        "modify it or write\nyour own script",
        "normally add a matching useful `reward_<suffix>`",
        "normally use the best productive",
        "Planner suggestions are aids, not constraints",
        "need not have been found by `plan.py`",
        "supporting model",
        "python commit.py",
        "What the trusted Critic does",
        "do not delete",
    ):
        assert required in prompt
    assert "actions_<suffix>" not in prompt
    assert "delete `.git`" not in prompt
    assert "Domain-defined action objects" in prompt


def test_actor_turn_prompts_match_new_protocol():
    first = _actor_turn_prompt(1, {})
    revision = _actor_turn_prompt(2, {"feedback": "Prediction contradicted."})
    assert "complete runbook" in first
    assert "plan" in first
    assert "reward_<suffix>" in first
    assert "read the detailed guide in plan.py" in first
    assert "adapt that planner (or your own script)" in first
    assert "commit.py" in first
    assert "do not execute the real environment" in first
    assert "trusted-report.json" in revision
    assert "one new clean commit" in revision
    assert revision.endswith("Prediction contradicted.")


def test_actor_files_and_instruments_are_self_contained(tmp_path):
    write_actor_files(
        tmp_path,
        (state(0),),
        "Toy domain.",
        planner_actions=({"action": 1},),
        max_depth=3,
        max_nodes=41,
    )
    (tmp_path / "world_model.py").write_text(MODEL)
    (tmp_path / "plan.json").write_text(json.dumps(trajectory(0, 1, 2)))
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
        "max_depth": 3,
        "max_nodes": 41,
        "planner_actions": [{"action": 1}],
    }
    assert json.loads((tmp_path / "plan-report.json").read_text())["validation"][
        "valid"
    ]
    planner = (tmp_path / "plan.py").read_text()
    assert "eggopt" not in planner
    assert "arcagi3" not in planner
    assert "def astar_search" in planner
    assert "subprocess.Popen" not in planner
    assert (tmp_path / "INSTRUCTIONS.md").read_text().endswith("Toy domain.\n")


def test_actor_files_include_domain_helpers(tmp_path):
    write_actor_files(
        tmp_path,
        (state(0),),
        domain_files=(("inspect_state.py", "print('domain helper')\n"),),
    )

    assert (tmp_path / "inspect_state.py").read_text() == "print('domain helper')\n"


def test_critic_feedback_explains_mismatch_and_exhaustion():
    mismatch = _execution_feedback("wrong_prediction", ["b"])
    assert "permanently appended" in mismatch
    assert "['b']" in mismatch
    exhausted = _execution_feedback("plan_exhausted")
    assert "did not report the goal" in exhausted


def test_evaluation_paths_and_schema(tmp_path):
    head = "a" * 40
    assert _evaluation_report_path(head) == f".trusted/evaluations/{head}.json"
    assert _evaluation_request_path(head) == f".trusted/requests/{head}.json"
    with pytest.raises(ValueError, match="full hexadecimal"):
        _evaluation_report_path("../escape")
    path = tmp_path / "report.json"
    path.write_text('{"backtest": {}}')
    with pytest.raises(TypeError, match="plan_validation"):
        _evaluation_report(path)


def test_physics_requires_all_domain_ports():
    actor = Agent(object(), {"role": "actor"})
    with pytest.raises(TypeError, match="observe"):
        PhysicsStrategy(
            actor=actor,
            observe="bad",
            execute=lambda **_: Value({}),
            validate_action=lambda **_: None,
            is_goal=lambda _: False,
            identity={"domain": "x"},
        )

    with pytest.raises(TypeError, match="terminal_outcome"):
        PhysicsStrategy(
            actor=actor,
            observe=lambda **_: Value({}),
            execute=lambda **_: Value({}),
            validate_action=lambda **_: None,
            is_goal=lambda _: False,
            identity={"domain": "x"},
            terminal_outcome="bad",
        )

    with pytest.raises(ValueError, match="non-empty"):
        TerminalOutcome("")


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


def _completed_scheduler_turn_with_live_lease(tmp_path, invoke_id):
    from eggthreads import append_message, create_root_thread

    db = ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    thread_id = create_root_thread(db, name="Actor")
    prompt_id = append_message(db, thread_id, "user", "go")
    after_seq = db.max_event_seq(thread_id)
    assert db.try_open_stream(
        thread_id,
        invoke_id,
        "2999-01-01 00:00:00",
        purpose="llm",
    )
    writer = db.invocation_writer(thread_id, invoke_id)
    writer.append_event(
        event_id="answer-event",
        type_="msg.create",
        msg_id="answer-message",
        payload={"role": "assistant", "content": "done"},
    )
    writer.close(event_id="close-event")
    return db, thread_id, prompt_id, after_seq, writer


@pytest.mark.parametrize("lease_end", ["release", "expire"])
def test_scheduler_managed_wait_observes_lease_end_without_new_event(
    tmp_path, lease_end
):
    import asyncio

    from eggopt.actor_critic import _wait_until_waiting

    invoke_id = "finishing-invoke"
    db, thread_id, prompt_id, after_seq, writer = (
        _completed_scheduler_turn_with_live_lease(tmp_path, invoke_id)
    )

    async def finish_turn():
        await asyncio.sleep(0.2)
        event_seq = db.max_event_seq(thread_id)
        if lease_end == "release":
            writer.release()
        else:
            assert db.heartbeat(thread_id, invoke_id, "2000-01-01 00:00:00")
        assert db.max_event_seq(thread_id) == event_seq

    async def exercise():
        finisher = asyncio.create_task(finish_turn())
        try:
            await asyncio.wait_for(
                _wait_until_waiting(
                    db,
                    thread_id,
                    prompt_id,
                    after_seq,
                    None,
                ),
                timeout=1,
            )
        finally:
            await finisher

    try:
        asyncio.run(exercise())
    finally:
        assert db.current_open(thread_id) is None
        db.close()


def test_scheduler_managed_wait_does_not_reduce_state_on_heartbeat(
    tmp_path, monkeypatch
):
    import asyncio

    from eggopt.actor_critic import _wait_until_waiting

    invoke_id = "heartbeat-invoke"
    db, thread_id, prompt_id, after_seq, _writer = (
        _completed_scheduler_turn_with_live_lease(tmp_path, invoke_id)
    )
    calls = []

    def state(_db, observed_thread_id):
        calls.append(observed_thread_id)
        return "running"

    monkeypatch.setattr("eggopt.actor_critic.thread_state", state)

    async def heartbeat():
        await asyncio.sleep(0.2)
        assert db.heartbeat(thread_id, invoke_id, "2999-01-02 00:00:00")

    async def exercise():
        heartbeat_task = asyncio.create_task(heartbeat())
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
                    timeout=0.5,
                )
        finally:
            await heartbeat_task

    try:
        asyncio.run(exercise())
        assert calls == [thread_id]
    finally:
        db.release(thread_id, invoke_id)
        db.close()


@pytest.mark.parametrize(
    "domain_files, error",
    [
        ([('helper.py', 'pass\n')], TypeError),
        ((("nested/helper.py", "pass\n"),), ValueError),
        ((("nested\\helper.py", "pass\n"),), ValueError),
        ((("plan.py", "pass\n"),), ValueError),
        ((("helper.py", "one\n"), ("helper.py", "two\n")), ValueError),
    ],
)
def test_physics_rejects_invalid_domain_files(domain_files, error):
    with pytest.raises(error, match="domain_files"):
        PhysicsStrategy(
            actor=Agent(object(), {"role": "actor"}),
            observe=lambda: None,
            execute=lambda: None,
            validate_action=lambda *_: None,
            is_goal=lambda *_: False,
            identity={"domain": "toy"},
            domain_files=domain_files,
        )


def test_valid_actor_repository_helpers_are_actor_owned(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    workspace = tmp_path / "run" / "workspace" / "innerContext"
    plan = trajectory(0, 1, 2)

    def edit(_call):
        (workspace / "plan.py").write_text("# Actor-owned custom planner\n")
        (workspace / "backtest.py").unlink()
        (workspace / "custom_search.py").write_text("# custom search\n")
        write_plan(workspace, plan)

    physics, _actor = strategy(workspace, edit)
    result = physics.run(run_dir="run", max_cycles=1)

    assert result.accepted is True
    assert (workspace / "plan.py").read_text() == "# Actor-owned custom planner\n"
    assert not (workspace / "backtest.py").exists()
    assert (workspace / "custom_search.py").read_text() == "# custom search\n"


def test_state_sync_does_not_refresh_actor_owned_files(tmp_path):
    from eggopt.physics.critic import sync_state

    (tmp_path / "plan.py").write_text("# custom planner\n")
    (tmp_path / "INSTRUCTIONS.md").write_text("custom instructions\n")
    sync_state(
        tmp_path,
        tmp_path,
        (state(0),),
        0,
        None,
        "new domain text",
        planner_actions=({"action": 1},),
        max_depth=4,
        max_nodes=100,
        evaluator_timeout_sec=17,
    )

    assert (tmp_path / "plan.py").read_text() == "# custom planner\n"
    assert (tmp_path / "INSTRUCTIONS.md").read_text() == "custom instructions\n"
    assert not (tmp_path / "backtest.py").exists()
    assert not (tmp_path / "world_model.py").exists()
    assert not (tmp_path / "plan.json").exists()
    assert not (tmp_path / "physics-config.json").exists()
    assert (tmp_path / "canonical-input.json").is_file()


def test_commit_validates_current_plan_and_creates_clean_head(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.name", "Physics")
    git(tmp_path, "config", "user.email", "physics@test")
    write_actor_files(
        tmp_path,
        (state(0),),
        planner_actions=({"action": 1},),
    )
    (tmp_path / "world_model.py").write_text(MODEL)
    (tmp_path / "plan.json").write_text(json.dumps(trajectory(0, 1)))
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-m", "initial")
    (tmp_path / "plan.json").write_text(json.dumps(trajectory(0, 1, 2)))

    completed = subprocess.run(
        ["python", "-E", "commit.py"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert git(tmp_path, "log", "-1", "--format=%s") == "Actor submits trajectory"
    assert git(tmp_path, "status", "--short") == ""


def test_evaluator_rejects_step_argument_mutation():
    source = """
def step_a(state, action):
    state["position"] += 1
    return state
"""
    result = run_evaluator(
        {
            "source": source,
            "timeline": [state(0)],
            "plan": trajectory(0, 1),
            "planner_actions": [],
            "max_depth": 2,
            "max_nodes": 20,
        }
    )
    assert result["plan_validation"]["valid"] is False
    assert "no Timeline-consistent" in result["plan_validation"]["error"]
    assert result["plan_validation"]["predictions"][0]["a"] is None
    assert "must not mutate" in result["plan_validation"]["model_errors"][0]["a"]


def test_valid_plan_acceptance_is_independent_of_advisory_planner(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    workspace = tmp_path / "run" / "workspace" / "innerContext"
    plan = trajectory(0, 1, 2)

    def edit(_call):
        write_plan(workspace, plan)

    physics, _actor = strategy(workspace, edit)
    object.__setattr__(physics, "planner_actions", ())
    result = physics.run(run_dir="run", max_cycles=1)

    assert result.accepted is True
    assert result.stopping_reason == "won"
    assert result.value["report"]["planning"]["suggestions"] == []


def test_domain_terminal_state_stops_after_real_transition(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    workspace = tmp_path / "run" / "workspace" / "innerContext"
    plan = trajectory(0, 1)

    def edit(_call):
        write_plan(workspace, plan)

    physics, actor = strategy(
        workspace,
        edit,
        execute=lambda **_: Value(state(-1)),
        terminal_outcome=lambda value: (
            TerminalOutcome("game_over") if value["position"] == -1 else None
        ),
    )
    result = physics.run(run_dir="run", max_cycles=3)

    assert result.accepted is True
    assert result.stopping_reason == "game_over"
    assert result.goal_reached is False
    assert result.actions == 1
    assert result.value["report"]["resolution"] == "game_over"
    assert result.timeline[-1]["next_state"] == state(-1)
    assert actor.calls == 1


def test_goal_state_stops_even_when_prediction_was_wrong(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    workspace = tmp_path / "run" / "workspace" / "innerContext"

    def edit(_call):
        write_plan(workspace, trajectory(0, 1))

    physics, actor = strategy(
        workspace,
        edit,
        execute=lambda **_: Value(state(2)),
    )
    result = physics.run(run_dir="run", max_cycles=3)

    assert result.accepted is True
    assert result.stopping_reason == "won"
    assert result.goal_reached is True
    assert result.actions == 1
    assert result.value["report"]["resolution"] == "won"
    assert actor.calls == 1


def test_initial_domain_terminal_state_stops_before_actor_turn(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    workspace = tmp_path / "run" / "workspace" / "innerContext"
    physics, actor = strategy(
        workspace,
        None,
        terminal_outcome=lambda value: (
            TerminalOutcome("game_over") if value["position"] == 0 else None
        ),
    )

    result = physics.run(run_dir="run", max_cycles=3)

    assert result.accepted is True
    assert result.stopping_reason == "game_over"
    assert result.goal_reached is False
    assert result.actions == 0
    assert result.rounds == 0
    assert result.critic_thread_id is None
    assert result.actor_thread_id is None
    assert actor.calls == 0


def test_domain_terminal_outcome_must_be_typed(tmp_path, monkeypatch):
    from eggflow import TaskError

    monkeypatch.chdir(tmp_path)
    workspace = tmp_path / "run" / "workspace" / "innerContext"

    def edit(_call):
        write_plan(workspace, trajectory(0, 1))

    physics, _actor = strategy(
        workspace,
        edit,
        execute=lambda **_: Value(state(1)),
        terminal_outcome=lambda _value: "game_over",
    )

    with pytest.raises(TaskError, match="TerminalOutcome or None"):
        physics.run(run_dir="run", max_cycles=1)


def test_one_broken_model_does_not_block_another_supporting_model():
    source = """
def step_broken(state, action):
    raise ValueError("unknown mechanism")
def step_good(state, action):
    return {"position": state["position"] + 1, "legal_actions": [1]}
"""
    result = run_evaluator(
        {
            "source": source,
            "timeline": [state(0)],
            "plan": trajectory(0, 1),
            "planner_actions": [],
            "max_depth": 2,
            "max_nodes": 20,
        }
    )
    assert result["plan_validation"]["valid"] is True
    assert result["plan_validation"]["supporting_models"] == ["good"]
    assert result["plan_validation"]["predictions"][0]["broken"] is None
    assert result["plan_validation"]["model_errors"][0]["broken"] == (
        "unknown mechanism"
    )


def test_submitted_plan_length_is_bounded_but_not_rediscovered():
    plan = trajectory(0, 1, 2)
    result = run_evaluator(
        {
            "source": MODEL,
            "timeline": [state(0)],
            "plan": plan,
            "planner_actions": [],
            "max_depth": 1,
            "max_nodes": 1,
        }
    )
    assert result["plan_validation"]["valid"] is False
    assert "limit is 1" in result["plan_validation"]["error"]


def test_structured_domain_action_is_preserved_in_trajectory_validation():
    click = {"action": 6, "data": {"x": 12, "y": 34}}
    source = """
from copy import deepcopy
def step_click(state, action):
    result = deepcopy(state)
    result["received"] = action
    return result
"""
    initial = {"received": None}
    predicted = {"received": click}
    plan = [{"state": initial, "action": click, "next_state": predicted}]
    result = run_evaluator(
        {
            "source": source,
            "timeline": [initial],
            "plan": plan,
            "planner_actions": [],
            "max_depth": 1,
            "max_nodes": 20,
        }
    )
    assert result["plan_validation"]["valid"] is True
    assert result["plan_validation"]["plan"][0]["action"] == click


def test_nested_parent_git_repository_is_not_a_physics_repository(tmp_path):
    from eggopt.physics.strategy import _git, _valid_repository

    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.name", "Parent")
    git(tmp_path, "config", "user.email", "parent@test")
    (tmp_path / "parent.txt").write_text("parent")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-m", "parent")
    nested = tmp_path / "runs" / "physics" / "workspace" / "innerContext"
    nested.mkdir(parents=True)

    assert git(nested, "rev-parse", "--show-toplevel") == str(tmp_path)
    assert _valid_repository(nested) is False
    with pytest.raises(RuntimeError, match="not an exact repository root"):
        _git(nested, "status", "--short")


def test_physics_initializes_nested_repository_instead_of_using_parent_git(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.name", "Parent")
    git(tmp_path, "config", "user.email", "parent@test")
    (tmp_path / "unrelated.txt").write_text("parent dirty")
    workspace = tmp_path / "run" / "workspace" / "innerContext"
    plan = trajectory(0, 1, 2)

    def edit(_call):
        assert (workspace / ".git").is_dir()
        assert git(workspace, "rev-parse", "--show-toplevel") == str(workspace)
        write_plan(workspace, plan)

    physics, _actor = strategy(workspace, edit)
    result = physics.run(run_dir="run", max_cycles=1)

    assert result.accepted is True
    assert (workspace / ".git").is_dir()
    assert git(workspace, "status", "--short") == ""
    assert (tmp_path / "unrelated.txt").read_text() == "parent dirty"


def test_thread_isolation_requires_exact_working_directory_and_sandbox(
    tmp_path, monkeypatch
):
    from eggopt.physics.strategy import _require_thread_isolation

    from eggthreads import (
        create_root_thread,
        set_thread_sandbox_config,
        set_thread_working_directory,
    )

    monkeypatch.chdir(tmp_path)
    db = ThreadsDB(tmp_path / ".egg" / "threads.sqlite")
    db.init_schema()
    thread_id = create_root_thread(db, name="isolated")
    repository = tmp_path / "repository"
    repository.mkdir()
    try:
        set_thread_working_directory(db, thread_id, str(repository))
        set_thread_sandbox_config(
            db,
            thread_id,
            enabled=True,
            provider="docker",
            settings={"filesystem": {"allowWrite": ["."]}},
        )
        _require_thread_isolation(db, thread_id, repository, role="Test")
        with pytest.raises(RuntimeError, match="escaped"):
            _require_thread_isolation(
                db, thread_id, tmp_path / "other", role="Test"
            )
        set_thread_sandbox_config(
            db,
            thread_id,
            enabled=False,
            provider="docker",
            settings={"filesystem": {"allowWrite": ["."]}},
        )
        with pytest.raises(RuntimeError, match="no enabled"):
            _require_thread_isolation(db, thread_id, repository, role="Test")
    finally:
        db.close()


def test_critic_python_exec_uses_eggthreads_sandbox_and_exact_repository(
    tmp_path, monkeypatch
):
    import asyncio
    import shutil

    from eggthreads import (
        create_default_tools,
        create_root_thread,
        set_thread_sandbox_config,
        set_thread_working_directory,
    )

    if shutil.which("docker") is None:
        pytest.skip("Docker is unavailable")
    completed = subprocess.run(
        ["docker", "info"], capture_output=True, text=True, timeout=10, check=False
    )
    if completed.returncode:
        pytest.skip("Docker daemon is unavailable")

    monkeypatch.chdir(tmp_path)
    repository = tmp_path / "critic-repository"
    repository.mkdir()
    (repository / "visible.txt").write_text("inside")
    (tmp_path / "outside.txt").write_text("outside")
    db = ThreadsDB(tmp_path / ".egg" / "threads.sqlite")
    db.init_schema()
    thread_id = create_root_thread(db, name="Critic")
    try:
        set_thread_working_directory(db, thread_id, str(repository))
        set_thread_sandbox_config(
            db,
            thread_id,
            enabled=True,
            provider="docker",
            settings={
                "network": {"allowedDomains": [], "deniedDomains": []},
                "workspace": "/workspace",
                "filesystem": {
                    "allowWrite": ["."],
                    "denyWrite": [".egg"],
                    "denyRead": [".egg"],
                },
                "extra_mounts": [],
                "extra_args": ["--cap-drop", "ALL"],
            },
            user_control_enabled=False,
        )

        async def execute():
            return await create_default_tools().execute_in_thread_context(
                "python_exec",
                {
                    "script": (
                        "import json, os\n"
                        "from pathlib import Path\n"
                        "print(json.dumps({"
                        "'cwd': os.getcwd(), "
                        "'inside': Path('visible.txt').read_text(), "
                        "'outside': Path('../outside.txt').exists()}))"
                    ),
                    "timeout": 30,
                },
                thread_id=thread_id,
                db=db,
                preserve_tool_result=True,
            )

        output = asyncio.run(execute())
        assert '"cwd": "/workspace"' in str(output)
        assert '"inside": "inside"' in str(output)
        assert '"outside": false' in str(output)
    finally:
        db.close()
