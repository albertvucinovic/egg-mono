from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from eggthreads import ThreadsDB, list_children_with_meta

from eggopt import (
    NativeGEPAConfig,
    current_evaluation,
    optimize_anything,
    plan_optimization,
)


class Evaluator:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, candidate, case):
        self.calls += 1
        level = int(candidate["instruction"])
        score = float(level >= case["target"])
        return score, {"target": case["target"], "level": level}


class Increment:
    def __init__(self) -> None:
        self.calls = 0
        self.requests = []

    def __call__(self, parents, evidence, objective):
        self.calls += 1
        self.requests.append((parents, evidence, objective))
        level = max(int(parent["instruction"]) for parent in parents) + 1
        return {"instruction": str(level)}


class ContextEvaluator(Evaluator):
    def __init__(self) -> None:
        super().__init__()
        self.contexts = []

    def __call__(self, candidate, case):
        self.contexts.append(dict(current_evaluation()))
        return super().__call__(candidate, case)


def config(tmp_path, evaluator, generator, **changes):
    base = NativeGEPAConfig(
        run_dir=tmp_path / "native",
        max_candidates=2,
        max_evaluator_calls=20,
        reflection_minibatch_size=1,
        parents_per_candidate=2,
        seed=1,
        evaluator_identity={"name": "threshold", "version": 1},
        case_id=lambda case: case["id"],
        generator=generator,
    )
    return replace(base, **changes)


def test_optimize_anything_is_case_wise_pareto_search(tmp_path):
    evaluator = Evaluator()
    generator = Increment()
    dataset = [
        {"id": "easy", "target": 1},
        {"id": "hard", "target": 2},
    ]

    result = optimize_anything(
        {"instruction": "0"},
        evaluator=evaluator,
        dataset=dataset,
        objective="Reach every target.",
        config=config(tmp_path, evaluator, generator),
    )

    assert result.best_candidate == {"instruction": "2"}
    assert result.best_score == 1.0
    assert result.case_scores == ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0))
    assert result.parents[0] == ()
    assert result.generated_candidates == 2
    assert result.evaluator_calls == evaluator.calls
    assert result.pareto_front == (1, 2)
    assert generator.calls == 2
    assert generator.requests[0][2] == "Reach every target."
    assert 1 <= len(generator.requests[1][0]) <= 2


def test_larger_limits_continue_without_repeating_cached_work(tmp_path):
    dataset = [
        {"id": "easy", "target": 1},
        {"id": "hard", "target": 2},
    ]
    first_evaluator = Evaluator()
    first_generator = Increment()
    first = optimize_anything(
        {"instruction": "0"},
        evaluator=first_evaluator,
        dataset=dataset,
        objective="Reach every target.",
        config=config(
            tmp_path,
            first_evaluator,
            first_generator,
            max_candidates=1,
        ),
    )

    continued_evaluator = Evaluator()
    continued_generator = Increment()
    continued = optimize_anything(
        {"instruction": "0"},
        evaluator=continued_evaluator,
        dataset=dataset,
        objective="Reach every target.",
        config=config(tmp_path, continued_evaluator, continued_generator),
    )

    assert first.best_candidate == {"instruction": "1"}
    assert continued.best_candidate == {"instruction": "2"}
    assert continued.generated_candidates == 2
    assert continued_generator.calls == 1
    assert continued_evaluator.calls < continued.evaluator_calls
    assert continued.evaluator_calls == first.evaluator_calls + continued_evaluator.calls


def test_budget_never_starts_an_evaluation_that_would_exceed_it(tmp_path):
    evaluator = Evaluator()
    generator = Increment()
    dataset = [
        {"id": "easy", "target": 1},
        {"id": "hard", "target": 2},
    ]

    result = optimize_anything(
        {"instruction": "0"},
        evaluator=evaluator,
        dataset=dataset,
        objective="Reach every target.",
        config=config(
            tmp_path,
            evaluator,
            generator,
            max_candidates=10,
            max_evaluator_calls=3,
        ),
    )

    assert result.evaluator_calls <= 3
    assert evaluator.calls <= 3


def test_evaluation_hierarchy_and_outer_inner_context_are_automatic(tmp_path):
    evaluator = ContextEvaluator()
    generator = Increment()
    dataset = [{"id": "easy", "target": 1}]
    cfg = config(
        tmp_path,
        evaluator,
        generator,
        max_candidates=1,
        parents_per_candidate=1,
    )

    optimize_anything(
        {"instruction": "0"},
        evaluator=evaluator,
        dataset=dataset,
        objective="Reach every target.",
        config=cfg,
    )

    assert evaluator.contexts
    context = evaluator.contexts[0]
    assert context["inner_context"] == context["outer_context"] + "/innerContext"
    assert (tmp_path / "native" / "workspaces").is_dir()

    db = ThreadsDB(tmp_path / "native" / ".egg" / "threads.sqlite")
    try:
        mutation = db.conn.execute(
            "SELECT thread_id FROM events WHERE type='eggopt.study'"
        ).fetchone()[0]
        assert db.get_thread(mutation).name == "Mutation"
        candidates = list_children_with_meta(db, mutation)
        assert candidates[0][1] == "Candidate 1 Evaluation"
        cases = list_children_with_meta(db, candidates[0][0])
        assert cases[0][1] == "easy Evaluation"
    finally:
        db.conn.close()


def test_plan_reports_total_and_incremental_cost():
    plan = plan_optimization(
        dataset_size=20,
        valset_size=20,
        max_candidates=5,
        max_evaluator_calls=100,
        reflection_minibatch_size=3,
        completed_candidates=2,
        completed_evaluator_calls=46,
    )

    assert plan.minibatch_size == 3
    assert plan.generated_candidates == 3
    assert plan.full_evaluations == 4
    assert plan.minibatch_evaluations == 3
    assert plan.evaluator_calls == 89
    assert plan.additional_generated_candidates == 1
    assert plan.additional_evaluator_calls == 43


class ScriptedAgentLLM:
    current_model_key = "scripted"

    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = 0

    def set_model(self, key):
        self.current_model_key = key

    def set_model_with_config(self, key, _config):
        self.current_model_key = key

    async def astream_chat(self, _messages, **_kwargs):
        self.calls += 1
        content = next(self.replies)
        yield {
            "type": "message",
            "role": "assistant",
            "content": content,
            "stop_reason": "end_turn",
        }


def test_actor_critic_reuses_pair_and_returns_latest_answer(tmp_path, monkeypatch):
    from eggflow import Task
    from eggopt import ActorCritic, Agent
    from eggthreads import ToolRegistry

    monkeypatch.chdir(tmp_path)
    run_dir = Path("run") / "actor-critic"

    actor_llm = ScriptedAgentLLM(["not json", '{"action":"LONG"}'])
    critic_llm = ScriptedAgentLLM(
        [
            '{"decision":"revise","feedback":"Return strict JSON."}',
            '{"decision":"accept","feedback":"Valid."}',
        ]
    )

    class ActorCriticEvaluator:
        def task(self, _candidate, _case):
            return EvaluateWithActorCritic()

    class EvaluateWithActorCritic(Task):
        def run(self):
            result = yield ActorCritic(
                actor=Agent(actor_llm, {"role": "actor"}, ToolRegistry()),
                critic=Agent(critic_llm, {"role": "critic"}, ToolRegistry()),
                actor_prompt=lambda round_number, state: (
                    "Predict." if round_number == 1 else state["feedback"]
                ),
                critic_prompt=lambda _round_number, state: (
                    f"Check this answer: {state['answer']}"
                ),
                max_rounds=2,
            )
            return 1.0, {
                "answer": result.answer,
                "accepted": result.accepted,
                "rounds": result.rounds,
            }

    result = optimize_anything(
        {"instruction": "0"},
        evaluator=ActorCriticEvaluator(),
        dataset=[{"id": "one"}],
        objective="Produce valid JSON.",
        config=NativeGEPAConfig(
            run_dir=run_dir,
            max_candidates=1,
            max_evaluator_calls=1,
            generator=Increment(),
            evaluator_identity={"name": "actor-critic-test"},
            case_id=lambda case: case["id"],
        ),
    )

    assert result.feedback[0][0] == {
        "answer": '{"action":"LONG"}',
        "accepted": True,
        "rounds": 2,
    }
    assert actor_llm.calls == critic_llm.calls == 2

    replay_actor = ScriptedAgentLLM([])
    replay_critic = ScriptedAgentLLM([])

    class ReplayEvaluator:
        def task(self, _candidate, _case):
            return ReplayTask()

    class ReplayTask(Task):
        def run(self):
            result = yield ActorCritic(
                actor=Agent(replay_actor, {"role": "actor"}, ToolRegistry()),
                critic=Agent(replay_critic, {"role": "critic"}, ToolRegistry()),
                actor_prompt=lambda round_number, state: (
                    "Predict." if round_number == 1 else state["feedback"]
                ),
                critic_prompt=lambda _round_number, state: (
                    f"Check this answer: {state['answer']}"
                ),
                max_rounds=2,
            )
            return 1.0, {"answer": result.answer}

    replayed = optimize_anything(
        {"instruction": "0"},
        evaluator=ReplayEvaluator(),
        dataset=[{"id": "one"}],
        objective="Produce valid JSON.",
        config=NativeGEPAConfig(
            run_dir=run_dir,
            max_candidates=1,
            max_evaluator_calls=1,
            generator=Increment(),
            evaluator_identity={"name": "actor-critic-test"},
            case_id=lambda case: case["id"],
        ),
    )
    assert replayed.best_score == 1.0
    assert replay_actor.calls == replay_critic.calls == 0

    db = ThreadsDB(run_dir / ".egg" / "threads.sqlite")
    try:
        evaluation_id = result.feedback[0][0]  # prove result remained plain data
        del evaluation_id
        pair = db.conn.execute(
            "SELECT name FROM threads WHERE name IN ('Actor', 'Critic') ORDER BY name"
        ).fetchall()
        assert [row[0] for row in pair] == ["Actor", "Critic"]
    finally:
        db.conn.close()


def test_valset_is_distinct_and_default_dataset_mode_matches_it(tmp_path):
    evaluator = Evaluator()
    generator = Increment()
    train = [{"id": "train", "target": 1}]
    validation = [{"id": "validation", "target": 2}]

    result = optimize_anything(
        {"instruction": "0"},
        evaluator=evaluator,
        dataset=train,
        valset=validation,
        objective="Reach validation target.",
        config=config(
            tmp_path,
            evaluator,
            generator,
            max_candidates=1,
            parents_per_candidate=1,
        ),
    )

    assert result.case_scores[0] == (0.0,)
    assert any(request[1][0]["cases"][0]["case"] == "train" for request in generator.requests)


def test_parent_selection_is_distinct_weighted_and_reproducible():
    import asyncio

    from eggflow import FlowExecutor, TaskStore
    from eggopt import SelectParents

    scores = ((1.0, 0.0), (0.0, 1.0), (1.0, 1.0))
    task = SelectParents(scores, count=3, seed=17, generation=4)
    first = asyncio.run(FlowExecutor(TaskStore(":memory:")).run(task))
    second = asyncio.run(FlowExecutor(TaskStore(":memory:")).run(task))

    assert first == second
    assert len(first) == len(set(first)) == 3


def test_async_evaluator_is_cached_without_extra_api_types(tmp_path):
    calls = 0

    async def evaluate(candidate, case):
        nonlocal calls
        calls += 1
        return float(int(candidate["instruction"]) >= case["target"]), {"async": True}

    generator = Increment()
    cfg = NativeGEPAConfig(
        run_dir=tmp_path / "async",
        max_candidates=1,
        max_evaluator_calls=1,
        generator=generator,
        evaluator_identity={"name": "async-test"},
        case_id=lambda case: case["id"],
    )
    kwargs = {
        "evaluator": evaluate,
        "dataset": [{"id": "one", "target": 0}],
        "objective": "Pass.",
        "config": cfg,
    }

    first = optimize_anything({"instruction": "0"}, **kwargs)
    second = optimize_anything({"instruction": "0"}, **kwargs)

    assert first.feedback == second.feedback == (({"async": True},),)
    assert calls == 1
