from __future__ import annotations

from dataclasses import dataclass

import pytest

from eggflow import Task
from eggopt import (
    ActorCritic,
    Agent,
    PhysicsEffect,
    PhysicsStrategy,
    current_operation,
)
from eggthreads import (
    ThreadsDB,
    build_tool_call_states,
    list_children_with_meta,
    list_root_threads,
    list_threads,
)


class Calls:
    def __init__(self):
        self.values = []


@dataclass
class Value(Task):
    name: str
    value: object
    calls: Calls

    def get_cache_key(self):
        from eggopt.identity import digest_payload

        return digest_payload(
            "test.physics.value.v1", {"name": self.name, "value": self.value}
        )

    def run(self):
        self.calls.values.append((self.name, dict(current_operation())))
        return self.value


def _toy_strategy(calls, executed):
    def observe(**_):
        return Value("observe", {"position": 0}, calls)

    def hypothesize(*, timeline, **_):
        position = timeline[-1]["position"]
        return Value("hypothesize", {"next": position + 1}, calls)

    def test(*, hypotheses, timeline, commitment, **_):
        if commitment is None:
            feedback = None
        else:
            expected = commitment["prediction"]
            feedback = (
                None
                if timeline[-1]["position"] == expected
                else {"counterexample": timeline[-1]}
            )
        return Value("test", feedback, calls)

    def deliberate(*, timeline, **_):
        position = timeline[-1]["position"]
        if position >= 2:
            value = None
        else:
            # The second intent is deliberately wrong. Reality must abort it.
            value = (
                {"action": 1, "prediction": position + 1},
                {"action": 99, "prediction": 100},
            )
        return Value("deliberate", value, calls)

    def execute(*, timeline, intent, **_):
        position = timeline[-1]["position"]

        @dataclass
        class Execute(Task):
            def get_cache_key(self):
                from eggopt.identity import digest_payload

                return digest_payload(
                    "test.physics.execute.v1",
                    {"position": position, "intent": intent},
                )

            def run(self):
                executed.append(intent["action"])
                # The first action succeeds; the second contradicts its prediction.
                next_position = position + 1
                return {"position": next_position}

        return Execute()

    return PhysicsStrategy(
        observe=observe,
        hypothesize=hypothesize,
        test=test,
        deliberate=deliberate,
        execute=execute,
        identity={"name": "toy", "version": 1},
    )


def test_physics_runs_tasks_aborts_queue_and_replays(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = Calls()
    executed = []
    strategy = _toy_strategy(calls, executed)

    result = strategy.run(run_dir="run", max_actions=5, max_cycles=5)

    assert result.stopping_reason == "deliberated"
    assert result.timeline == (
        {"position": 0},
        {"position": 1},
        {"position": 2},
    )
    assert result.actions == 2
    assert executed == [1, 99]
    assert all(
        call[1]["physics_thread_id"] == result.physics_thread_id
        for call in calls.values
    )
    assert {call[1]["outer_context"] for call in calls.values} == {
        str(tmp_path / "run" / "workspaces" / "environment"),
        str(tmp_path / "run" / "workspaces" / "hypotheses"),
        str(tmp_path / "run" / "workspaces" / "plan"),
    }

    replay_calls = Calls()
    replay_executed = []
    replay = _toy_strategy(replay_calls, replay_executed).run(
        run_dir="run", max_actions=5, max_cycles=5
    )
    assert replay.timeline == result.timeline
    assert replay_calls.values == []
    assert replay_executed == []

    db = ThreadsDB(tmp_path / "run" / ".egg" / "threads.sqlite")
    try:
        roots = list_root_threads(db)
        assert len(roots) == 1
        root = next(
            thread for thread in list_threads(db) if thread.thread_id == roots[0]
        )
        assert root.name == "Physics"
        assert sorted(
            name for _, name, *_ in list_children_with_meta(db, roots[0])
        ) == [
            "Environment",
            "Hypotheses",
            "Plan",
        ]
    finally:
        db.close()


def test_physics_requires_task_factories(tmp_path):
    strategy = PhysicsStrategy(
        observe=lambda **_: "not a task",
        hypothesize=lambda **_: Value("h", {}, Calls()),
        test=lambda **_: Value("t", None, Calls()),
        deliberate=lambda **_: Value("d", None, Calls()),
        execute=lambda **_: Value("e", {}, Calls()),
        identity={"name": "invalid"},
    )

    with pytest.raises(Exception, match="observe must construct an Eggflow Task"):
        strategy.run(run_dir=tmp_path / "run", max_cycles=1)


def test_physics_effect_records_one_environment_thread_history(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = Calls()

    def effect(name, value, *, thread_id):
        return PhysicsEffect(
            Value(name, value, calls),
            thread_id,
            name,
            {"value": value},
        )

    strategy = PhysicsStrategy(
        observe=lambda thread_id, **_: effect(
            "observe", {"position": 0}, thread_id=thread_id
        ),
        hypothesize=lambda **_: Value("hypothesize", {"next": 1}, calls),
        test=lambda **_: Value("test", None, calls),
        deliberate=lambda timeline, **_: Value(
            "deliberate",
            None
            if len(timeline) > 1
            else ({"action": 1, "prediction": {"position": 1}},),
            calls,
        ),
        execute=lambda thread_id, **_: effect(
            "act", {"position": 1}, thread_id=thread_id
        ),
        identity={"name": "effect-history"},
    )

    result = strategy.run(run_dir="run", max_actions=2)
    db = ThreadsDB(tmp_path / "run" / ".egg" / "threads.sqlite")
    try:
        calls_by_id = build_tool_call_states(db, result.environment_thread_id)
        assert [call.name for call in calls_by_id.values()] == ["observe", "act"]
    finally:
        db.close()


class ScriptedLLM:
    current_model_key = "test-model"

    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = 0

    def set_model(self, key):
        self.current_model_key = key

    def set_model_with_config(self, key, _config):
        self.current_model_key = key

    async def astream_chat(self, _messages, **_kwargs):
        self.calls += 1
        yield {
            "type": "message",
            "role": "assistant",
            "content": next(self.replies),
            "stop_reason": "end_turn",
        }


@dataclass
class Accept(Task):
    def run(self):
        return {"decision": "accept", "feedback": "accepted"}


def test_physics_role_can_compose_actor_critic_on_persistent_thread(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    modeler = ScriptedLLM(['{"models":["H1","H2","H3"]}'])

    @dataclass
    class Hypothesize(Task):
        def run(self):
            result = yield ActorCritic(
                actor=Agent(modeler, {"role": "physics-modeler"}),
                critic=Accept(),
                actor_prompt=lambda _round, _state: "Propose distinct hypotheses.",
                max_rounds=1,
                names=("Modeler", "Backtest"),
            )
            return result.answer

    strategy = PhysicsStrategy(
        observe=lambda **_: Value("observe", {"position": 0}, Calls()),
        hypothesize=lambda **_: Hypothesize(),
        test=lambda **_: Value("test", None, Calls()),
        deliberate=lambda **_: Value("deliberate", None, Calls()),
        execute=lambda **_: Value("execute", {}, Calls()),
        identity={"name": "actor-critic-physics", "version": 1},
    )

    result = strategy.run(run_dir="run", max_cycles=1)

    assert result.hypotheses == '{"models":["H1","H2","H3"]}'
    assert modeler.calls == 1

    db = ThreadsDB(tmp_path / "run" / ".egg" / "threads.sqlite")
    try:
        hypothesis_children = list_children_with_meta(db, result.hypotheses_thread_id)
        assert [name for _, name, *_ in hypothesis_children] == ["Backtest"]
        backtest_id = hypothesis_children[0][0]
        assert [name for _, name, *_ in list_children_with_meta(db, backtest_id)] == [
            "Modeler"
        ]
    finally:
        db.close()

    replay_modeler = ScriptedLLM([])

    @dataclass
    class ReplayHypothesize(Task):
        def run(self):
            result = yield ActorCritic(
                actor=Agent(replay_modeler, {"role": "physics-modeler"}),
                critic=Accept(),
                actor_prompt=lambda _round, _state: "Propose distinct hypotheses.",
                max_rounds=1,
                names=("Modeler", "Backtest"),
            )
            return result.answer

    replay = PhysicsStrategy(
        observe=lambda **_: Value("observe", {"position": 0}, Calls()),
        hypothesize=lambda **_: ReplayHypothesize(),
        test=lambda **_: Value("test", None, Calls()),
        deliberate=lambda **_: Value("deliberate", None, Calls()),
        execute=lambda **_: Value("execute", {}, Calls()),
        identity={"name": "actor-critic-physics", "version": 1},
    ).run(run_dir="run", max_cycles=1)

    assert replay.hypotheses == result.hypotheses
    assert replay_modeler.calls == 0


def test_actor_critic_prompt_task_runs_after_actor_thread_assignment(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    actor = ScriptedLLM(["ready"])
    observed = []

    @dataclass
    class PreparePrompt(Task):
        actor_thread_id: str

        def run(self):
            observed.append(self.actor_thread_id)
            return "Inspect the prepared data."

    @dataclass
    class Hypothesize(Task):
        def run(self):
            result = yield ActorCritic(
                actor=Agent(actor, {"role": "prepared-physics-modeler"}),
                critic=Accept(),
                actor_prompt=lambda _round, state: PreparePrompt(
                    state["actor_thread_id"]
                ),
                max_rounds=1,
                names=("Modeler", "Backtest"),
            )
            return result.answer

    result = PhysicsStrategy(
        observe=lambda **_: Value("observe", {"position": 0}, Calls()),
        hypothesize=lambda **_: Hypothesize(),
        test=lambda **_: Value("test", None, Calls()),
        deliberate=lambda **_: Value("deliberate", None, Calls()),
        execute=lambda **_: Value("execute", {}, Calls()),
        identity={"test": "actor-critic-prompt-task"},
    ).run(run_dir="run", max_cycles=1)

    db = ThreadsDB(tmp_path / "run" / ".egg" / "threads.sqlite")
    try:
        backtest_id = list_children_with_meta(db, result.hypotheses_thread_id)[0][0]
        modeler_id = list_children_with_meta(db, backtest_id)[0][0]
        assert observed == [modeler_id]
    finally:
        db.close()

    replay_actor = ScriptedLLM([])

    @dataclass
    class ReplayHypothesize(Task):
        def run(self):
            result = yield ActorCritic(
                actor=Agent(replay_actor, {"role": "prepared-physics-modeler"}),
                critic=Accept(),
                actor_prompt=lambda _round, state: PreparePrompt(
                    state["actor_thread_id"]
                ),
                max_rounds=1,
                names=("Modeler", "Backtest"),
            )
            return result.answer

    PhysicsStrategy(
        observe=lambda **_: Value("observe", {"position": 0}, Calls()),
        hypothesize=lambda **_: ReplayHypothesize(),
        test=lambda **_: Value("test", None, Calls()),
        deliberate=lambda **_: Value("deliberate", None, Calls()),
        execute=lambda **_: Value("execute", {}, Calls()),
        identity={"test": "actor-critic-prompt-task"},
    ).run(run_dir="run", max_cycles=1)

    assert observed == [modeler_id]
    assert replay_actor.calls == 0


def test_actor_critic_rejects_prompt_task_with_non_text_result(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    @dataclass
    class NotText(Task):
        def run(self):
            return {"prompt": "wrong type"}

    @dataclass
    class Hypothesize(Task):
        def run(self):
            return (
                yield ActorCritic(
                    actor=Agent(ScriptedLLM([]), {"role": "invalid-prompt"}),
                    critic=Accept(),
                    actor_prompt=lambda _round, _state: NotText(),
                    max_rounds=1,
                )
            )

    strategy = PhysicsStrategy(
        observe=lambda **_: Value("observe", {"position": 0}, Calls()),
        hypothesize=lambda **_: Hypothesize(),
        test=lambda **_: Value("test", None, Calls()),
        deliberate=lambda **_: Value("deliberate", None, Calls()),
        execute=lambda **_: Value("execute", {}, Calls()),
        identity={"test": "invalid-actor-critic-prompt-task"},
    )

    with pytest.raises(Exception, match="actor prompt must resolve to a string"):
        strategy.run(run_dir="run", max_cycles=1)
