from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from eggthreads import (
    ThreadsDB,
    get_thread_tools_config,
    is_descendant_thread,
    list_children_with_meta,
    list_root_threads,
    list_threads,
    load_thread_projection,
)

from eggopt import (
    Agent,
    GEPAConfig,
    ThreadTool,
    current_evaluation,
    optimize_anything,
    plan_optimization,
)
from eggopt.tools import SAFE_TOOLS


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


def test_agent_defaults_to_safe_tools_and_accepts_explicit_replacement():
    from eggthreads import ToolRegistry

    default = Agent(object(), {"role": "default"})
    assert default.allowed_tools == SAFE_TOOLS
    assert {item["function"]["name"] for item in default.tools.tools_spec()}.issuperset(
        SAFE_TOOLS
    )

    restricted = Agent(
        object(),
        {"role": "restricted"},
        allowed_tools=frozenset({"python_exec"}),
    )
    assert restricted.allowed_tools == {"python_exec"}

    expanded = Agent(
        object(),
        {"role": "expanded"},
        allowed_tools=frozenset({"web_search"}),
    )
    assert expanded.allowed_tools == {"web_search"}

    custom_tools = ToolRegistry()
    custom_tools.register(
        "domain_probe",
        "Domain-owned tool",
        {"type": "object", "properties": {}},
        lambda _args: "ok",
    )
    custom = Agent(
        object(),
        {"role": "custom"},
        tools=custom_tools,
        allowed_tools=frozenset({"domain_probe"}),
    )
    assert custom.allowed_tools == {"domain_probe"}


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_agent_context_limit_must_be_a_positive_integer(limit):
    with pytest.raises(ValueError, match="context_limit"):
        Agent(
            object(),
            {"role": "invalid-context-budget"},
            context_limit=limit,
        )


def test_agent_separates_full_context_budget_from_eggthreads_runner_config():
    from eggthreads import RunnerConfig

    agent = Agent(
        object(),
        {"role": "full-context-budget"},
        runner_config=RunnerConfig(auto_compact_threshold_tokens=700),
        context_limit=9_000,
    )

    assert agent.context_limit == 9_000
    assert agent.runner_config.context_limit is None
    assert agent.runner_config.auto_compact_threshold_tokens == 700
    with pytest.raises(ValueError, match="provider-context limit"):
        Agent(
            object(),
            {"role": "wrong-context-budget"},
            runner_config=RunnerConfig(context_limit=9_000),
        )


def test_agent_can_opt_into_tool_auto_approval():
    agent = Agent(
        object(),
        {"role": "auto-approved-tools"},
        auto_approve_tools=True,
    )

    assert agent.auto_approve_tools is True


def test_default_mutation_prompt_explains_validation_and_selection_feedback():
    from eggopt.gepa import DEFAULT_MUTATION_SYSTEM_PROMPT

    assert "full-validation score history" in DEFAULT_MUTATION_SYSTEM_PROMPT
    assert "parent-selection rationale" in DEFAULT_MUTATION_SYSTEM_PROMPT


def test_thread_tool_reuses_public_synthetic_tool_lifecycle(tmp_path, monkeypatch):
    import json

    from eggflow import Task
    from eggthreads import ToolRegistry, list_tool_calls_for_thread

    monkeypatch.chdir(tmp_path)
    registry = ToolRegistry()
    calls = []
    registry.register(
        "echo",
        "Echo",
        {"type": "object", "properties": {"value": {"type": "string"}}},
        lambda arguments: calls.append(arguments["value"]) or arguments["value"],
    )

    class Evaluator:
        def task(self, _candidate, _case):
            return UseTool()

    class UseTool(Task):
        def run(self):
            context = current_evaluation()
            value = yield ThreadTool(
                registry,
                context["evaluation_thread_id"],
                "echo",
                {"value": "ok"},
            )
            return 1.0, {"value": value}

    config = GEPAConfig(
        run_dir=tmp_path / "thread-tool",
        max_candidates=1,
        max_evaluator_calls=1,
        generator=Increment(),
        evaluator_identity={"name": "thread-tool-test"},
        case_id=lambda case: case["id"],
    )
    first = optimize_anything(
        {"instruction": "0"},
        evaluator=Evaluator(),
        dataset=[{"id": "one"}],
        objective="Echo.",
        config=config,
    )
    second = optimize_anything(
        {"instruction": "0"},
        evaluator=Evaluator(),
        dataset=[{"id": "one"}],
        objective="Echo.",
        config=config,
    )

    assert first.feedback == second.feedback == (({"value": "ok"},),)
    assert calls == ["ok"]
    db = ThreadsDB(tmp_path / "thread-tool" / ".egg" / "threads.sqlite")
    try:
        thread_id = next(
            thread.thread_id
            for thread in list_threads(db)
            if thread.name == "one Evaluation"
        )
        tool_calls = list_tool_calls_for_thread(db, thread_id)
        assert len(tool_calls) == 1
        assert tool_calls[0].name == "echo"
        assert json.loads(tool_calls[0].arguments) == {"value": "ok"}
        assert tool_calls[0].published is True
    finally:
        db.conn.close()


def test_thread_tool_recovers_existing_recorded_call(tmp_path, monkeypatch):
    from eggflow import Task
    from eggthreads import ToolRegistry, list_tool_calls_for_thread

    monkeypatch.chdir(tmp_path)
    registry = ToolRegistry()
    calls = []
    registry.register(
        "echo",
        "Echo",
        {"type": "object", "properties": {"value": {"type": "string"}}},
        lambda arguments: calls.append(arguments["value"]) or arguments["value"],
    )

    class Evaluator:
        def task(self, _candidate, _case):
            return UseTool()

    class UseTool(Task):
        def run(self):
            context = current_evaluation()
            task = ThreadTool(
                registry,
                context["evaluation_thread_id"],
                "echo",
                {"value": "resume"},
            )
            from eggthreads import record_synthetic_user_tool_call

            db = ThreadsDB(
                tmp_path / "thread-tool-recovery" / ".egg" / "threads.sqlite"
            )
            try:
                record_synthetic_user_tool_call(
                    db,
                    context["evaluation_thread_id"],
                    "echo",
                    {"value": "resume"},
                    "resume",
                    origin="eggopt",
                    tool_call_id=task.get_cache_key().rsplit(":", 1)[-1],
                )
            finally:
                db.conn.close()
            return 1.0, {"value": (yield task)}

    result = optimize_anything(
        {"instruction": "0"},
        evaluator=Evaluator(),
        dataset=[{"id": "one"}],
        objective="Echo.",
        config=GEPAConfig(
            run_dir=tmp_path / "thread-tool-recovery",
            max_candidates=1,
            max_evaluator_calls=1,
            generator=Increment(),
            evaluator_identity={"name": "thread-tool-recovery-test"},
            case_id=lambda case: case["id"],
        ),
    )

    assert result.feedback == (({"value": "resume"},),)
    assert calls == []
    db = ThreadsDB(tmp_path / "thread-tool-recovery" / ".egg" / "threads.sqlite")
    try:
        thread_id = next(
            thread.thread_id
            for thread in list_threads(db)
            if thread.name == "one Evaluation"
        )
        assert len(list_tool_calls_for_thread(db, thread_id)) == 1
    finally:
        db.conn.close()


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_gepa_evaluator_context_limit_must_be_positive(limit):
    with pytest.raises(ValueError, match="evaluator_context_limit"):
        GEPAConfig(evaluator_context_limit=limit)


def test_gepa_progress_must_be_callable():
    with pytest.raises(TypeError, match="progress"):
        GEPAConfig(progress="verbose")


def config(tmp_path, evaluator, generator, **changes):
    base = GEPAConfig(
        run_dir=tmp_path / "native",
        max_candidates=2,
        max_evaluator_calls=20,
        mutation_minibatch_size=1,
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
        config=config(
            tmp_path,
            evaluator,
            generator,
            evaluator_context_limit=9_000,
        ),
    )

    assert result.best_candidate == {"instruction": "2"}
    assert result.best_score == 1.0
    assert result.case_scores == ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0))
    assert result.parents[0] == ()
    assert result.generated_candidates == 2
    assert result.evaluator_calls == evaluator.calls
    assert result.per_validation_case_best_candidate_indices == (
        ("easy", (1, 2)),
        ("hard", (2,)),
    )
    assert generator.calls == 2
    assert generator.requests[0][2] == "Reach every target."
    assert 1 <= len(generator.requests[1][0]) <= 2

    evaluator_calls = evaluator.calls
    replay = optimize_anything(
        {"instruction": "0"},
        evaluator=evaluator,
        dataset=dataset,
        objective="Reach every target.",
        config=config(
            tmp_path,
            evaluator,
            generator,
            evaluator_context_limit=9_000,
        ),
    )
    assert replay == result
    assert evaluator.calls == evaluator_calls
    assert generator.calls == 2

    from eggthreads import get_context_limit

    db = ThreadsDB(tmp_path / "native" / ".egg" / "threads.sqlite")
    try:
        case_ids = [
            thread.thread_id
            for thread in list_threads(db)
            if thread.name
            and thread.name.endswith(" Evaluation")
            and not thread.name.startswith("Candidate ")
        ]
        assert case_ids
        assert all(get_context_limit(db, thread_id) == 9_000 for thread_id in case_ids)
    finally:
        db.conn.close()


def test_validation_case_identities_must_be_unique(tmp_path):
    evaluator = Evaluator()

    with pytest.raises(ValueError, match="valset case identities must be unique"):
        optimize_anything(
            {"instruction": "0"},
            evaluator=evaluator,
            dataset=[{"id": "train", "target": 0}],
            valset=[
                {"id": "duplicate", "target": 0},
                {"id": "duplicate", "target": 1},
            ],
            objective="Reach every target.",
            config=config(
                tmp_path,
                evaluator,
                Increment(),
                max_candidates=1,
                parents_per_candidate=1,
            ),
        )


def test_minibatch_acceptance_can_send_ties_to_full_validation(tmp_path):
    dataset = [{"id": "already-passing", "target": 0}]

    strict = optimize_anything(
        {"instruction": "0"},
        evaluator=Evaluator(),
        dataset=dataset,
        objective="Keep passing.",
        config=config(
            tmp_path / "strict",
            Evaluator(),
            Increment(),
            max_candidates=1,
            parents_per_candidate=1,
        ),
    )
    allow_equal = optimize_anything(
        {"instruction": "0"},
        evaluator=Evaluator(),
        dataset=dataset,
        objective="Keep passing.",
        config=config(
            tmp_path / "allow-equal",
            Evaluator(),
            Increment(),
            max_candidates=1,
            parents_per_candidate=1,
            minibatch_acceptance="improvement_or_equal",
        ),
    )

    assert strict.candidates == ({"instruction": "0"},)
    assert allow_equal.candidates == (
        {"instruction": "0"},
        {"instruction": "1"},
    )


def test_next_mutation_reports_last_candidate_rejected_on_minibatch(
    tmp_path, monkeypatch
):
    import json

    from eggopt import Mutator

    monkeypatch.chdir(tmp_path)
    llm = ScriptedMutationLLM(
        [
            json.dumps({"mutations": [{"instruction": "-1"}]}),
            json.dumps({"mutations": [{"instruction": "1"}]}),
        ]
    )
    optimize_anything(
        {"instruction": "0"},
        evaluator=Evaluator(),
        dataset=[{"id": "train", "target": 1}],
        objective="Reach the target.",
        config=GEPAConfig(
            run_dir=tmp_path / "rejected-candidate-feedback",
            max_candidates=2,
            max_evaluator_calls=10,
            mutation_minibatch_size=1,
            parents_per_candidate=1,
            mutator=Mutator.eggthreads(
                llm=llm,
                identity={"model": "rejected-candidate-feedback"},
                instruction="Improve the instruction.",
                allowed_tools=set(),
            ),
            evaluator_identity={"name": "rejected-candidate-feedback"},
            case_id=lambda case: case["id"],
        ),
    )

    workspace = tmp_path / "rejected-candidate-feedback" / "workspaces" / "mutation"
    requests = [
        json.loads(path.read_text())
        for path in sorted(workspace.glob("feedback-*.json"))
    ]
    last_results = [request.get("last_candidate_result") for request in requests]
    rejected = next(result for result in last_results if result is not None)
    assert len(requests) == 2

    assert rejected == {
        "full_validation": None,
        "minibatch": {
            "acceptance_policy": "strict_improvement",
            "accepted": False,
            "aggregate_score": 0.0,
            "case_count": 1,
            "parent_envelope_aggregate_score": 0.0,
        },
        "mutation_generation": 1,
        "outcome": "rejected_on_minibatch",
    }


def test_minibatch_acceptance_rejects_unknown_policy():
    with pytest.raises(ValueError, match="minibatch_acceptance"):
        GEPAConfig(minibatch_acceptance="unknown")


def test_progress_callback_reports_each_case_and_candidate(tmp_path):
    events = []
    evaluator = Evaluator()
    generator = Increment()
    options = config(
        tmp_path,
        evaluator,
        generator,
        max_candidates=1,
        parents_per_candidate=1,
        progress=events.append,
    )

    optimize_anything(
        {"instruction": "0"},
        evaluator=evaluator,
        dataset=[{"id": "easy", "target": 1}],
        objective="Reach the target.",
        config=options,
    )

    replay_events = []
    replay = optimize_anything(
        {"instruction": "0"},
        evaluator=Evaluator(),
        dataset=[{"id": "easy", "target": 1}],
        objective="Reach the target.",
        config=replace(options, progress=replay_events.append),
    )

    assert [event["kind"] for event in events] == [
        item
        for _ in range(4)
        for item in (
            "candidate_evaluation_started",
            "case_evaluation",
            "candidate_evaluation",
        )
    ]
    assert replay_events == events
    assert evaluator.calls == 4
    assert events[0]["case_count"] == 1
    assert events[0]["candidate_thread_name"] == "Candidate 1 Evaluation"
    assert events[0]["candidate_number"] == 1
    assert events[0]["proposal_number"] is None
    assert events[0]["evaluation_role"] == "candidate_validation"
    assert events[1]["case"] == "easy"
    names_by_thread = {}
    for event in events:
        thread_id = event["candidate_thread_id"]
        names_by_thread.setdefault(thread_id, event["candidate_thread_name"])
        assert event["candidate_thread_name"] == names_by_thread[thread_id]
    assert events[1]["case_number"] == 1
    assert events[1]["case_count"] == 1
    assert events[-1]["kind"] == "candidate_evaluation"
    assert events[-1]["stage"] == "full"
    assert replay.best_candidate == {"instruction": "1"}


def test_progress_distinguishes_proposals_from_admitted_candidates(tmp_path):
    events = []
    evaluator = Evaluator()
    result = optimize_anything(
        {"instruction": "0"},
        evaluator=evaluator,
        dataset=[{"id": "easy", "target": 1}],
        objective="Reach the target.",
        config=config(
            tmp_path,
            evaluator,
            Increment(),
            max_candidates=1,
            parents_per_candidate=1,
            progress=events.append,
        ),
    )

    starts = [event for event in events if event["kind"] == "candidate_evaluation_started"]
    assert [event["evaluation_role"] for event in starts] == [
        "candidate_validation",
        "parent_reflection",
        "proposal_minibatch",
        "candidate_validation",
    ]
    assert starts[1]["candidate_number"] == 1
    assert starts[1]["proposal_number"] == 1
    assert starts[2]["candidate_number"] is None
    assert starts[2]["proposal_number"] == 1
    assert starts[2]["candidate_thread_name"] == "Proposal 1 Minibatch"
    assert starts[3]["candidate_number"] == 2
    assert starts[3]["proposal_number"] == 1
    assert result.generated_candidates == 1
    assert len(result.candidates) == 2


def test_progress_projects_cached_results_on_resume(tmp_path):
    evaluator = Evaluator()
    generator = Increment()
    options = config(
        tmp_path,
        evaluator,
        generator,
        max_candidates=1,
        parents_per_candidate=1,
    )
    arguments = {
        "evaluator": evaluator,
        "dataset": [{"id": "easy", "target": 1}],
        "objective": "Reach the target.",
    }

    optimize_anything({"instruction": "0"}, config=options, **arguments)
    replay_events = []
    optimize_anything(
        {"instruction": "0"},
        config=replace(options, progress=replay_events.append),
        **arguments,
    )

    assert [event["kind"] for event in replay_events] == [
        item
        for _ in range(4)
        for item in (
            "candidate_evaluation_started",
            "case_evaluation",
            "candidate_evaluation",
        )
    ]


def test_gepa_results_live_only_in_eggflow(tmp_path):
    optimize_anything(
        {"instruction": "0"},
        evaluator=Evaluator(),
        dataset=[{"id": "easy", "target": 1}],
        objective="Reach the target.",
        config=config(
            tmp_path,
            Evaluator(),
            Increment(),
            max_candidates=1,
            parents_per_candidate=1,
        ),
    )

    db = ThreadsDB(tmp_path / "native" / ".egg" / "threads.sqlite")
    try:
        assert (
            db.conn.execute(
                "SELECT COUNT(*) FROM events WHERE type LIKE 'eggopt.gepa.%-result.v1' "
                "OR type='eggopt.gepa.progress.v1'"
            ).fetchone()[0]
            == 0
        )
        assert (
            db.conn.execute(
                "SELECT COUNT(*) FROM events WHERE type='msg.create' "
                "AND json_extract(payload_json, '$.eggopt_kind') LIKE 'eggopt.gepa.%'"
            ).fetchone()[0]
            == 0
        )
    finally:
        db.conn.close()


def test_study_identity_is_cached_without_an_eggthreads_marker(tmp_path):
    options = config(
        tmp_path,
        Evaluator(),
        Increment(),
        max_candidates=1,
        parents_per_candidate=1,
    )
    arguments = {
        "seed_candidate": {"instruction": "0"},
        "evaluator": Evaluator(),
        "dataset": [{"id": "easy", "target": 1}],
        "objective": "Reach the target.",
        "config": options,
    }

    optimize_anything(**arguments)
    optimize_anything(**arguments)

    db = ThreadsDB(tmp_path / "native" / ".egg" / "threads.sqlite")
    try:
        assert len(list_root_threads(db)) == 1
        assert (
            db.conn.execute(
                "SELECT COUNT(*) FROM events WHERE type='eggopt.study'"
            ).fetchone()[0]
            == 0
        )
    finally:
        db.conn.close()


def test_async_custom_generator_uses_shared_await_task(tmp_path):
    async def generate(_parents, _evidence, _objective):
        return {"instruction": "1"}

    result = optimize_anything(
        {"instruction": "0"},
        evaluator=Evaluator(),
        dataset=[{"id": "easy", "target": 1}],
        objective="Reach the target.",
        config=config(
            tmp_path,
            Evaluator(),
            generate,
            max_candidates=1,
            parents_per_candidate=1,
        ),
    )

    assert result.best_candidate == {"instruction": "1"}


def test_changed_stopping_budgets_continue_without_invalidating_cached_work(tmp_path):
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
            max_evaluator_calls=6,
        ),
    )

    continued_evaluator = Evaluator()
    continued_generator = Increment()
    continued = optimize_anything(
        {"instruction": "0"},
        evaluator=continued_evaluator,
        dataset=dataset,
        objective="Reach every target.",
        config=config(
            tmp_path,
            continued_evaluator,
            continued_generator,
            max_candidates=2,
            max_evaluator_calls=20,
        ),
    )

    assert first.best_candidate == {"instruction": "1"}
    assert continued.best_candidate == {"instruction": "2"}
    assert continued.generated_candidates == 2
    assert continued_generator.calls == 1
    assert continued_evaluator.calls < continued.evaluator_calls
    assert (
        continued.evaluator_calls == first.evaluator_calls + continued_evaluator.calls
    )


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


def test_evaluation_hierarchy_and_outer_inner_context_are_automatic(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
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
        study = list_root_threads(db)[0]
        assert db.get_thread(study).name == "GEPA"
        study_children = list_children_with_meta(db, study)
        validation = next(child for child in study_children if child[1] == "Validation")
        assert validation[1] == "Validation"
        mutation_review = next(
            child for child in study_children if child[1] == "Mutation Review"
        )
        mutation = list_children_with_meta(db, mutation_review[0])[0]
        assert mutation[1] == "Mutation"
        reflection = list_children_with_meta(db, mutation[0])[0]
        assert reflection[1] == "Reflection"

        validation_candidates = list_children_with_meta(db, validation[0])
        assert validation_candidates[0][1] == "Candidate 1 Evaluation"
        assert not is_descendant_thread(db, mutation[0], validation_candidates[0][0])

        reflection_candidates = list_children_with_meta(db, reflection[0])
        assert reflection_candidates[0][1] == "Candidate 1 Reflection for Proposal 1"
        assert is_descendant_thread(db, mutation[0], reflection_candidates[0][0])
        assert get_thread_tools_config(db, reflection_candidates[0][0]).is_tool_allowed(
            "send_message_to_child"
        )
        cases = list_children_with_meta(db, reflection_candidates[0][0])
        assert cases[0][1] == "easy Evaluation"
    finally:
        db.conn.close()


def test_plan_reports_total_and_incremental_cost():
    plan = plan_optimization(
        dataset_size=20,
        valset_size=20,
        max_candidates=5,
        max_evaluator_calls=100,
        mutation_minibatch_size=3,
        completed_candidates=2,
        completed_evaluator_calls=46,
    )

    assert plan.minibatch_size == 3
    assert plan.generated_candidates == 3
    assert plan.full_evaluations == 4
    assert plan.minibatch_evaluations == 6
    assert plan.evaluator_calls == 98
    assert plan.additional_generated_candidates == 1
    assert plan.additional_evaluator_calls == 52


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


class ReasoningOnlyAgentLLM(ScriptedAgentLLM):
    async def astream_chat(self, _messages, **_kwargs):
        self.calls += 1
        content = next(self.replies)
        yield {
            "type": "message",
            "role": "assistant",
            "content": content,
            "reasoning": "internal reasoning",
            "stop_reason": "end_turn",
        }


def test_actor_critic_recovery_continues_interrupted_turn(tmp_path, monkeypatch):
    from eggflow import Task
    from eggopt import ActorCritic, Agent
    from eggthreads import append_message, create_child_thread, create_root_thread

    monkeypatch.chdir(tmp_path)
    db = ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    evaluation_id = create_root_thread(db, name="Evaluation")
    critic_id = create_child_thread(db, evaluation_id, name="Critic")
    actor_id = create_child_thread(db, critic_id, name="Actor")
    trigger_id = append_message(
        db,
        actor_id,
        "user",
        "Answer.",
        extra={"eggopt_actor_critic_key": "turn-key"},
    )
    invocation = "interrupted-invoke"
    db.append_event(
        "interrupted-open",
        actor_id,
        "stream.open",
        {"stream_kind": "llm"},
        msg_id="interrupted-stream-message",
        invoke_id=invocation,
    )
    db.append_event(
        "interrupted-reasoning",
        actor_id,
        "stream.delta",
        {"reason": "partial reasoning"},
        invoke_id=invocation,
        chunk_seq=0,
    )
    db.append_event(
        "interrupted-close",
        actor_id,
        "stream.close",
        {},
        invoke_id=invocation,
    )

    @dataclass
    class Review(Task):
        def run(self):
            return {"decision": "accept", "feedback": "Valid."}

    interaction = ActorCritic(
        actor=Agent(object(), {"role": "interrupted"}),
        critic=Review(),
        actor_prompt=lambda _round, _state: "Answer.",
        max_rounds=1,
    )

    assert interaction.recover_interaction(db, evaluation_id, None) is True
    continuation = db.conn.execute(
        "SELECT payload_json FROM events WHERE thread_id=? AND type='control.interrupt' "
        "AND json_extract(payload_json, '$.purpose')='continue'",
        (actor_id,),
    ).fetchone()
    assert continuation is not None
    assert trigger_id in continuation[0]


def test_actor_critic_recover_uses_current_evaluation_runtime(tmp_path):
    from eggopt import ActorCritic, Agent
    from eggopt.context import _bind_evaluation_runtime, _evaluation_scope
    from eggthreads import append_message, create_child_thread, create_root_thread

    db = ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    evaluation_id = create_root_thread(db, name="Evaluation")
    critic_id = create_child_thread(db, evaluation_id, name="Critic")
    actor_id = create_child_thread(db, critic_id, name="Actor")
    trigger_id = append_message(
        db,
        actor_id,
        "user",
        "Answer.",
        extra={"eggopt_actor_critic_key": "turn-key"},
    )
    runtime_key = "actor-critic-recovery-runtime"
    _bind_evaluation_runtime(runtime_key, db)
    interaction = ActorCritic(
        actor=Agent(object(), {"role": "actor"}),
        critic=Agent(object(), {"role": "critic"}),
        actor_prompt=lambda _round, _state: "Answer.",
        critic_prompt=lambda _round, _state: "Review.",
        max_rounds=1,
    )
    with _evaluation_scope(
        {
            "evaluation_thread_id": evaluation_id,
            "_runtime_key": runtime_key,
            "_evaluation_key": "evaluation-key",
            "_context_limit": None,
        }
    ):
        cache_key = interaction.get_cache_key()
        assert interaction.recover() is True
        assert interaction.get_cache_key() == cache_key

    continuation = db.conn.execute(
        "SELECT payload_json FROM events WHERE thread_id=? AND type='control.interrupt' "
        "AND json_extract(payload_json, '$.purpose')='continue'",
        (actor_id,),
    ).fetchone()
    assert continuation is not None
    assert trigger_id in continuation[0]


def test_actor_critic_recovery_recovers_actor_and_agent_critic(tmp_path):
    from eggopt import ActorCritic, Agent
    from eggthreads import append_message, create_child_thread, create_root_thread

    db = ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    evaluation_id = create_root_thread(db, name="Evaluation")
    critic_id = create_child_thread(db, evaluation_id, name="Critic")
    actor_id = create_child_thread(db, critic_id, name="Actor")
    actor_trigger = append_message(
        db,
        actor_id,
        "user",
        "Answer.",
        extra={"eggopt_actor_critic_key": "actor-turn"},
    )
    critic_trigger = append_message(
        db,
        critic_id,
        "user",
        "Review.",
        extra={"eggopt_actor_critic_key": "critic-turn"},
    )
    interaction = ActorCritic(
        actor=Agent(object(), {"role": "actor"}),
        critic=Agent(object(), {"role": "critic"}),
        actor_prompt=lambda _round, _state: "Answer.",
        critic_prompt=lambda _round, _state: "Review.",
        max_rounds=1,
    )

    assert interaction.recover_interaction(db, evaluation_id, None) is True

    continuations = db.conn.execute(
        "SELECT thread_id, payload_json FROM events WHERE type='control.interrupt' "
        "AND json_extract(payload_json, '$.purpose')='continue'"
    ).fetchall()
    assert {row[0] for row in continuations} == {actor_id, critic_id}
    assert any(actor_trigger in row[1] for row in continuations)
    assert any(critic_trigger in row[1] for row in continuations)


def test_actor_critic_recovery_checks_both_agents_even_if_actor_refuses(
    tmp_path, monkeypatch
):
    from eggopt import ActorCritic, Agent
    from eggthreads import append_message, create_child_thread, create_root_thread

    db = ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    evaluation_id = create_root_thread(db, name="Evaluation")
    critic_id = create_child_thread(db, evaluation_id, name="Critic")
    actor_id = create_child_thread(db, critic_id, name="Actor")
    append_message(
        db,
        actor_id,
        "user",
        "Answer.",
        extra={"eggopt_actor_critic_key": "actor-turn"},
    )
    append_message(
        db,
        critic_id,
        "user",
        "Review.",
        extra={"eggopt_actor_critic_key": "critic-turn"},
    )
    recovered_threads = []

    def recover(interaction):
        recovered_threads.append(interaction.thread_id)
        return interaction.thread_id != actor_id

    monkeypatch.setattr("eggopt.actor_critic.InteractionRecovery.recover", recover)
    interaction = ActorCritic(
        actor=Agent(object(), {"role": "actor"}),
        critic=Agent(object(), {"role": "critic"}),
        actor_prompt=lambda _round, _state: "Answer.",
        critic_prompt=lambda _round, _state: "Review.",
        max_rounds=1,
    )

    assert interaction.recover_interaction(db, evaluation_id, None) is False
    assert recovered_threads == [actor_id, critic_id]


def test_actor_critic_recovery_repairs_structurally_unhealthy_turn(
    tmp_path, monkeypatch
):
    from eggflow import Task
    from eggopt import ActorCritic, Agent
    from eggthreads import append_message, create_child_thread, create_root_thread

    db = ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    evaluation_id = create_root_thread(db, name="Evaluation")
    critic_id = create_child_thread(db, evaluation_id, name="Critic")
    actor_id = create_child_thread(db, critic_id, name="Actor")
    trigger_id = append_message(
        db,
        actor_id,
        "user",
        "Answer.",
        extra={"eggopt_actor_critic_key": "turn-key"},
    )
    db.append_event(
        "interrupted-open",
        actor_id,
        "stream.open",
        {"stream_kind": "llm"},
        msg_id="interrupted-stream-message",
        invoke_id="unclosed-invoke",
    )

    @dataclass
    class Review(Task):
        def run(self):
            return {"decision": "accept", "feedback": "Valid."}

    interaction = ActorCritic(
        actor=Agent(object(), {"role": "interrupted"}),
        critic=Review(),
        actor_prompt=lambda _round, _state: "Answer.",
        max_rounds=1,
    )

    assert interaction.recover_interaction(db, evaluation_id, None) is True
    continuation = db.conn.execute(
        "SELECT payload_json FROM events WHERE thread_id=? AND type='control.interrupt' "
        "AND json_extract(payload_json, '$.purpose')='continue'",
        (actor_id,),
    ).fetchone()
    assert continuation is not None
    assert trigger_id in continuation[0]


def test_actor_critic_recovery_does_not_continue_completed_turn(tmp_path):
    from eggflow import Task
    from eggopt import ActorCritic, Agent
    from eggthreads import append_message, create_child_thread, create_root_thread

    db = ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    evaluation_id = create_root_thread(db, name="Evaluation")
    critic_id = create_child_thread(db, evaluation_id, name="Critic")
    actor_id = create_child_thread(db, critic_id, name="Actor")
    append_message(
        db,
        actor_id,
        "user",
        "Answer.",
        extra={"eggopt_actor_critic_key": "turn-key"},
    )
    append_message(db, actor_id, "assistant", "Complete answer.")

    @dataclass
    class Review(Task):
        def run(self):
            return {"decision": "accept", "feedback": "Valid."}

    interaction = ActorCritic(
        actor=Agent(object(), {"role": "complete"}),
        critic=Review(),
        actor_prompt=lambda _round, _state: "Answer.",
        max_rounds=1,
    )

    assert interaction.recover_interaction(db, evaluation_id, None) is True
    assert (
        db.conn.execute(
            "SELECT 1 FROM events WHERE thread_id=? AND type='control.interrupt'",
            (actor_id,),
        ).fetchone()
        is None
    )


def test_actor_critic_recovery_keeps_context_limit_terminal(tmp_path, monkeypatch):
    from eggflow import ContextLimitExceededError, Task
    from eggopt import ActorCritic, Agent
    from eggthreads import append_message, create_child_thread, create_root_thread

    db = ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    evaluation_id = create_root_thread(db, name="Evaluation")
    critic_id = create_child_thread(db, evaluation_id, name="Critic")
    actor_id = create_child_thread(db, critic_id, name="Actor")
    append_message(
        db,
        actor_id,
        "user",
        "Answer.",
        extra={"eggopt_actor_critic_key": "turn-key"},
    )
    monkeypatch.setattr(
        "eggopt.recovery.full_context_tokens", lambda _db, _thread_id: 100
    )

    @dataclass
    class Review(Task):
        def run(self):
            return {"decision": "accept", "feedback": "Valid."}

    interaction = ActorCritic(
        actor=Agent(object(), {"role": "limited"}),
        critic=Review(),
        actor_prompt=lambda _round, _state: "Answer.",
        max_rounds=1,
    )

    with pytest.raises(ContextLimitExceededError, match="100 >= 100"):
        interaction.recover_interaction(db, evaluation_id, 100)


def test_case_evaluation_delegates_failed_retry_recovery(tmp_path):
    import asyncio

    from eggopt.gepa.evaluation import _EvaluateCase
    from eggthreads import ThreadsDB

    calls = []

    class RecoverableEvaluator:
        def recover(self, candidate, case, *, evaluation_thread_id):
            calls.append((candidate, case, evaluation_thread_id))
            return True

    task = _EvaluateCase(
        RecoverableEvaluator(),
        {"instruction": "candidate"},
        {"id": "case"},
        {"name": "recoverable"},
        "case",
        ("evaluation-thread", str(tmp_path), "runtime-key"),
        ThreadsDB(tmp_path / "threads.sqlite"),
    )

    assert asyncio.run(task.recover()) is True
    assert calls == [
        ({"instruction": "candidate"}, {"id": "case"}, "evaluation-thread")
    ]


def test_actor_critic_sends_empty_final_answer_to_task_critic(tmp_path, monkeypatch):
    from eggflow import Task
    from eggopt import ActorCritic, Agent

    monkeypatch.chdir(tmp_path)
    actor = ReasoningOnlyAgentLLM(["", '{"answer":"valid"}'])
    reviews = []

    @dataclass
    class Review(Task):
        answer: str | None = None

        def run(self):
            reviews.append(self.answer)
            if self.answer:
                return {"decision": "accept", "feedback": "Valid."}
            return {"decision": "revise", "feedback": "Return a final answer."}

    class Evaluator:
        def task(self, _candidate, _case):
            return Evaluate()

    class Evaluate(Task):
        def run(self):
            result = yield ActorCritic(
                actor=Agent(actor, {"role": "reasoning-only"}),
                critic=Review(),
                actor_prompt=lambda _round, state: state["feedback"] or "Answer.",
                max_rounds=2,
            )
            return 1.0, {"answer": result.answer, "accepted": result.accepted}

    result = optimize_anything(
        {"instruction": "0"},
        evaluator=Evaluator(),
        dataset=[{"id": "one"}],
        objective="Produce an answer.",
        config=GEPAConfig(
            run_dir=tmp_path / "reasoning-only",
            max_candidates=1,
            max_evaluator_calls=1,
            generator=Increment(),
            evaluator_identity={"name": "reasoning-only-test"},
            case_id=lambda case: case["id"],
        ),
    )

    assert reviews == ["", '{"answer":"valid"}']
    assert result.feedback == (({"answer": '{"answer":"valid"}', "accepted": True},),)
    assert actor.calls == 2


def test_actor_critic_reuses_pair_and_returns_latest_answer(tmp_path, monkeypatch):
    from eggflow import Task
    from eggopt import ActorCritic, Agent
    from eggthreads import get_context_limit, get_thread_auto_approval_status

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
                actor=Agent(
                    actor_llm,
                    {"role": "actor"},
                    auto_approve_tools=True,
                ),
                critic=Agent(critic_llm, {"role": "critic"}),
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
        config=GEPAConfig(
            run_dir=run_dir,
            max_candidates=1,
            max_evaluator_calls=1,
            generator=Increment(),
            evaluator_identity={"name": "actor-critic-test"},
            case_id=lambda case: case["id"],
            evaluator_context_limit=9_000,
        ),
    )

    assert result.feedback[0][0] == {
        "answer": '{"action":"LONG"}',
        "accepted": True,
        "rounds": 2,
    }
    assert actor_llm.calls == critic_llm.calls == 2

    db = ThreadsDB(run_dir / ".egg" / "threads.sqlite")
    try:
        for name in ("Actor", "Critic"):
            thread_id = db.conn.execute(
                "SELECT thread_id FROM threads WHERE name=?", (name,)
            ).fetchone()[0]
            assert get_thread_tools_config(db, thread_id).allowed_tools == SAFE_TOOLS
            assert get_context_limit(db, thread_id) == 9_000
            if name == "Actor":
                assert get_thread_auto_approval_status(db, thread_id) is True
    finally:
        db.conn.close()

    replay_actor = ScriptedAgentLLM([])
    replay_critic = ScriptedAgentLLM([])

    class ReplayEvaluator:
        def task(self, _candidate, _case):
            return ReplayTask()

    class ReplayTask(Task):
        def run(self):
            result = yield ActorCritic(
                actor=Agent(replay_actor, {"role": "actor"}),
                critic=Agent(replay_critic, {"role": "critic"}),
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
        config=GEPAConfig(
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


def test_actor_critic_context_limit_is_typed_from_full_history(monkeypatch):
    import asyncio

    from eggflow import ContextLimitExceededError
    from eggopt.context_limit import run_with_full_context_limit

    class Runner:
        async def run_once(self):
            raise AssertionError("provider must not be called")

    monkeypatch.setattr(
        "eggopt.context_limit.thread_token_stats",
        lambda _db, _thread: {"context_tokens": 10, "full_thread_tokens": 100},
    )

    with pytest.raises(ContextLimitExceededError, match="100 >= 100"):
        asyncio.run(
            run_with_full_context_limit(
                Runner(), object(), "actor", 100, operation="ActorCritic agent"
            )
        )


def test_actor_critic_accepts_a_task_as_critic(tmp_path, monkeypatch):
    from eggflow import Task
    from eggopt import ActorCritic, Agent

    monkeypatch.chdir(tmp_path)
    run_dir = Path("run") / "task-critic"
    actor = ScriptedAgentLLM(["bad", "bad"])
    reviews = []

    @dataclass
    class Review(Task):
        actor_thread_id: str | None = None
        critic_thread_id: str | None = None

        answer: str | None = None
        round_number: int | None = None

        def run(self):
            reviews.append((self.answer, self.actor_thread_id, self.critic_thread_id))
            db = ThreadsDB(run_dir / ".egg" / "threads.sqlite")
            try:
                assert (
                    db.conn.execute(
                        "SELECT parent_id FROM children WHERE child_id=?",
                        (self.actor_thread_id,),
                    ).fetchone()[0]
                    == self.critic_thread_id
                )
            finally:
                db.conn.close()
            if self.round_number == 1:
                return {"decision": "revise", "feedback": "Return JSON."}
            return {"decision": "accept", "feedback": "Valid."}

    class Evaluator:
        def task(self, _candidate, _case):
            return Evaluate()

    class Evaluate(Task):
        def run(self):
            result = yield ActorCritic(
                actor=Agent(actor, {"role": "prediction"}),
                critic=Review(),
                actor_prompt=lambda _round, state: state["feedback"] or "Predict.",
                max_rounds=2,
                names=("Prediction", "Execution"),
            )
            return 1.0, {
                "answer": result.answer,
                "accepted": result.accepted,
                "rounds": result.rounds,
            }

    config = GEPAConfig(
        run_dir=run_dir,
        max_candidates=1,
        max_evaluator_calls=1,
        generator=Increment(),
        evaluator_identity={"name": "task-critic-test"},
        case_id=lambda case: case["id"],
    )
    result = optimize_anything(
        {"instruction": "0"},
        evaluator=Evaluator(),
        dataset=[{"id": "one"}],
        objective="Produce valid JSON.",
        config=config,
    )

    assert result.feedback[0][0] == {
        "answer": "bad",
        "accepted": True,
        "rounds": 2,
    }
    assert actor.calls == 2
    assert len(reviews) == 2
    assert reviews[0][1:] == reviews[1][1:]
    assert reviews[0][1] != reviews[0][2]

    db = ThreadsDB(run_dir / ".egg" / "threads.sqlite")
    try:
        assert [
            name
            for (name,) in db.conn.execute(
                "SELECT name FROM threads WHERE name IN ('Prediction','Execution') "
                "ORDER BY name"
            )
        ] == ["Execution", "Prediction"]
        actor_id = db.conn.execute(
            "SELECT thread_id FROM threads WHERE name='Prediction'"
        ).fetchone()[0]
        critic_id = db.conn.execute(
            "SELECT thread_id FROM threads WHERE name='Execution'"
        ).fetchone()[0]
        assert (
            db.conn.execute(
                "SELECT parent_id FROM children WHERE child_id=?", (actor_id,)
            ).fetchone()[0]
            == critic_id
        )
    finally:
        db.conn.close()

    replay_actor = ScriptedAgentLLM([])

    class ReplayEvaluator:
        def task(self, _candidate, _case):
            return Replay()

    class Replay(Task):
        def run(self):
            result = yield ActorCritic(
                actor=Agent(replay_actor, {"role": "prediction"}),
                critic=Review(),
                actor_prompt=lambda _round, state: state["feedback"] or "Predict.",
                max_rounds=2,
                names=("Prediction", "Execution"),
            )
            return 1.0, {"answer": result.answer}

    replay = optimize_anything(
        {"instruction": "0"},
        evaluator=ReplayEvaluator(),
        dataset=[{"id": "one"}],
        objective="Produce valid JSON.",
        config=config,
    )
    assert replay.best_score == 1.0
    assert replay_actor.calls == 0
    assert len(reviews) == 2


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
    assert any(
        request[1][0]["cases"][0]["case"] == "train" for request in generator.requests
    )

    db = ThreadsDB(tmp_path / "native" / ".egg" / "threads.sqlite")
    try:
        study = list_root_threads(db)[0]
        study_children = list_children_with_meta(db, study)
        validation_id = next(
            child_id
            for child_id, name, *_rest in study_children
            if name == "Validation"
        )
        mutation_review_id = next(
            child_id
            for child_id, name, *_rest in study_children
            if name == "Mutation Review"
        )
        mutation_id = next(
            child_id
            for child_id, name, *_rest in list_children_with_meta(
                db, mutation_review_id
            )
            if name == "Mutation"
        )
        reflection_id = next(
            child_id
            for child_id, name, *_rest in list_children_with_meta(db, mutation_id)
            if name == "Reflection"
        )

        validation_cases = {
            name
            for candidate_id, *_rest in list_children_with_meta(db, validation_id)
            for _case_id, name, *_rest in list_children_with_meta(db, candidate_id)
        }
        reflection_cases = {
            name
            for candidate_id, *_rest in list_children_with_meta(db, reflection_id)
            for _case_id, name, *_rest in list_children_with_meta(db, candidate_id)
        }

        assert validation_cases == {"validation Evaluation"}
        assert reflection_cases == {"train Evaluation"}
        assert not is_descendant_thread(db, mutation_id, validation_id)
        assert is_descendant_thread(db, mutation_id, reflection_id)
    finally:
        db.close()


def test_mutator_receives_full_validation_scores_and_selection_reason(tmp_path):
    evaluator = Evaluator()
    generator = Increment()
    dataset = [{"id": "train", "target": 1}]
    valset = [
        {"id": "validation-easy", "target": 1},
        {"id": "validation-hard", "target": 2},
    ]

    result = optimize_anything(
        {"instruction": "0"},
        evaluator=evaluator,
        dataset=dataset,
        valset=valset,
        objective="Reach validation targets.",
        config=config(
            tmp_path,
            evaluator,
            generator,
            max_candidates=2,
            max_evaluator_calls=30,
            parents_per_candidate=1,
            minibatch_acceptance="improvement_or_equal",
        ),
    )

    assert result.case_scores == ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0))
    first_evidence = generator.requests[0][1][0]
    second_evidence = generator.requests[1][1][0]
    assert first_evidence["selection_reason"] == (
        "Selected from the full-validation Pareto pool by deterministic weighted "
        "sampling; Candidate 1 was best or tied-best on 2 of 2 validation cases."
    )
    assert second_evidence["selection_reason"] == (
        "Selected from the full-validation Pareto pool by deterministic weighted "
        "sampling; Candidate 2 was best or tied-best on 2 of 2 validation cases."
    )
    assert "full_validation_scores" not in first_evidence


def test_native_mutation_prompt_references_feedback_files(tmp_path, monkeypatch):
    import json

    from eggopt import Mutator

    monkeypatch.chdir(tmp_path)
    llm = ScriptedMutationLLM(
        [
            json.dumps({"mutations": [{"instruction": "1"}]}),
            json.dumps({"mutations": [{"instruction": "2"}]}),
        ]
    )
    evaluator = Evaluator()
    optimize_anything(
        {"instruction": "0"},
        evaluator=evaluator,
        dataset=[{"id": "train", "target": 1}],
        valset=[
            {"id": "validation-easy", "target": 1},
            {"id": "validation-hard", "target": 2},
        ],
        objective="Reach validation targets.",
        config=GEPAConfig(
            run_dir=tmp_path / "mutation-validation-feedback",
            max_candidates=2,
            max_evaluator_calls=30,
            mutation_minibatch_size=1,
            parents_per_candidate=1,
            minibatch_acceptance="improvement_or_equal",
            mutator=Mutator.eggthreads(
                llm=llm,
                identity={"model": "validation-feedback"},
                instruction="Improve the instruction.",
                allowed_tools=set(),
            ),
            evaluator_identity={"name": "mutation-validation-feedback-test"},
            case_id=lambda case: case["id"],
        ),
    )

    db = ThreadsDB(
        tmp_path / "mutation-validation-feedback" / ".egg" / "threads.sqlite"
    )
    try:
        mutation_id = next(
            thread.thread_id for thread in list_threads(db) if thread.name == "Mutation"
        )
        prompts = [
            message.payload["content"]
            for message in load_thread_projection(db, mutation_id).messages
            if message.payload.get("role") == "user"
            and message.payload.get("eggopt_actor_critic_key")
        ]
        workspace = (
            tmp_path / "mutation-validation-feedback" / "workspaces" / "mutation"
        )
        feedback_files = sorted(workspace.glob("feedback-*.json"))
        by_name = {path.name: json.loads(path.read_text()) for path in feedback_files}
        requests = [
            next(value for name, value in by_name.items() if name in prompt)
            for prompt in prompts
        ]
    finally:
        db.close()

    assert len(prompts) == len(feedback_files) == 2
    assert all(len(prompt) < 1_000 for prompt in prompts)
    assert requests[0]["full_validation_scores"] == [
        {
            "aggregate_score": 0.0,
            "candidate_index": 0,
            "candidate_number": 1,
            "case_count": 2,
            "mutation_generation": None,
        }
    ]
    assert "last_candidate_result" not in requests[0]
    assert requests[1]["full_validation_scores"] == [
        requests[0]["full_validation_scores"][0],
        {
            "aggregate_score": 0.5,
            "candidate_index": 1,
            "candidate_number": 2,
            "case_count": 2,
            "mutation_generation": 1,
        },
    ]
    assert requests[1]["last_candidate_result"] == {
        "full_validation": {
            "aggregate_score": 0.5,
            "candidate_index": 1,
            "candidate_number": 2,
            "case_count": 2,
        },
        "minibatch": {
            "acceptance_policy": "improvement_or_equal",
            "accepted": True,
            "aggregate_score": 1.0,
            "case_count": 1,
            "parent_envelope_aggregate_score": 0.0,
        },
        "mutation_generation": 1,
        "outcome": "full_validation_completed_and_added",
    }
    assert "Your last candidate performed as follows" in prompts[1]
    assert "Now use the selected Pareto parents" in prompts[1]
    assert '"aggregate_score": 0.5' in prompts[1]
    assert "full_validation_score" not in requests[1]["evaluation_evidence"][0]
    assert (
        "best or tied-best" in requests[1]["evaluation_evidence"][0]["selection_reason"]
    )


def test_full_validation_scores_change_native_mutation_key():
    from eggopt.gepa import Mutate, Mutator

    mutator = Mutator(
        Agent(object(), {"model": "mutation-key"}),
        "Improve the candidate.",
    )
    arguments = (
        mutator,
        ({"instruction": "0"},),
        ({"parent_index": 0, "cases": []},),
        "Improve.",
        0,
    )
    first = Mutate(
        *arguments,
        (
            {
                "candidate_index": 0,
                "candidate_number": 1,
                "mutation_generation": None,
                "aggregate_score": 0.0,
                "case_count": 2,
            },
        ),
    )
    changed = Mutate(
        *arguments,
        (
            {
                "candidate_index": 0,
                "candidate_number": 1,
                "mutation_generation": None,
                "aggregate_score": 0.5,
                "case_count": 2,
            },
        ),
    )

    assert first.get_cache_key() != changed.get_cache_key()


def test_last_candidate_result_changes_native_mutation_key():
    from eggopt.gepa import Mutate, Mutator

    mutator = Mutator(
        Agent(object(), {"model": "last-candidate-key"}),
        "Improve the candidate.",
    )
    arguments = (
        mutator,
        ({"instruction": "0"},),
        ({"parent_index": 0, "cases": []},),
        "Improve.",
        1,
        ({"candidate_number": 1, "aggregate_score": 0.0},),
    )

    first = Mutate(
        *arguments,
        {"mutation_generation": 1, "outcome": "rejected_on_minibatch"},
    )
    changed = Mutate(
        *arguments,
        {"mutation_generation": 1, "outcome": "full_validation_completed_and_added"},
    )

    assert first.get_cache_key() != changed.get_cache_key()


def test_last_candidate_result_preserves_first_mutation_cache_identity():
    from eggopt.gepa import Mutate, Mutator

    mutator = Mutator(
        Agent(object(), {"model": "first-mutation-key"}),
        "Improve the candidate.",
    )
    arguments = (
        mutator,
        ({"instruction": "0"},),
        ({"parent_index": 0, "cases": []},),
        "Improve.",
        0,
        ({"candidate_number": 1, "aggregate_score": 0.0},),
    )

    assert (
        Mutate(*arguments).get_cache_key()
        == Mutate(*arguments, last_candidate_result=None).get_cache_key()
    )


def test_native_mutator_accepts_a_domain_critic_and_keys_its_identity():
    from eggflow import Task
    from eggopt import Mutator
    from eggopt.gepa import Mutate

    @dataclass
    class DomainCritic(Task):
        version: str
        answer: str | None = None

        def get_cache_key(self):
            return f"domain-critic:{self.version}"

        def run(self):
            return {"decision": "accept", "feedback": "Valid domain artifact."}

    base = {
        "llm": object(),
        "identity": {"model": "domain-critic"},
        "instruction": "Improve.",
        "allowed_tools": set(),
    }
    first = Mutator.eggthreads(**base, critic=DomainCritic("v1"))
    changed = Mutator.eggthreads(**base, critic=DomainCritic("v2"))
    arguments = (
        ({"instruction": "0"},),
        ({"parent_index": 0, "cases": []},),
        "Improve.",
        0,
    )

    assert first.critic is not None
    assert first.identity["critic"] == {
        "module": DomainCritic.__module__,
        "name": DomainCritic.__qualname__,
        "key": "domain-critic:v1",
    }
    assert (
        Mutate(first, *arguments).get_cache_key()
        != Mutate(changed, *arguments).get_cache_key()
    )


def test_native_mutator_domain_critic_factory_receives_selected_parent():
    from eggflow import Task
    from eggopt import Mutator
    from eggopt.gepa.mutation import Mutate

    @dataclass
    class Review(Task):
        parent: dict[str, str]

        def run(self):
            return {"decision": "accept", "feedback": "Valid."}

    def critic(parent):
        return Review(parent)

    mutator = Mutator.eggthreads(
        llm=object(),
        identity={"model": "critic-factory"},
        instruction="Improve.",
        allowed_tools=set(),
        critic=critic,
    )
    task = Mutate(
        mutator,
        ({"instruction": "selected"},),
        ({"parent_index": 0, "cases": []},),
        "Improve.",
        0,
    )

    assert mutator.identity["critic"] == {
        "module": critic.__module__,
        "name": critic.__qualname__,
    }
    assert task.mutator.critic({"instruction": "selected"}).parent == {
        "instruction": "selected"
    }


def test_native_mutation_key_includes_factory_produced_critic_task():
    from eggflow import Task
    from eggopt import Mutator
    from eggopt.gepa import Mutate

    @dataclass
    class Review(Task):
        version: str

        def get_cache_key(self):
            return f"review:{self.version}"

    @dataclass
    class Factory:
        version: str

        def __call__(self, _parent):
            return Review(self.version)

    base = {
        "llm": object(),
        "identity": {"model": "factory-key"},
        "instruction": "Improve.",
        "allowed_tools": set(),
    }
    arguments = (
        ({"instruction": "0"},),
        ({"parent_index": 0, "cases": []},),
        "Improve.",
        0,
    )
    first = Mutate(Mutator.eggthreads(**base, critic=Factory("v1")), *arguments)
    changed = Mutate(Mutator.eggthreads(**base, critic=Factory("v2")), *arguments)

    assert first.mutator.identity == changed.mutator.identity
    assert first.get_cache_key() != changed.get_cache_key()


def test_mutation_feedback_file_is_semantic_and_immutable(tmp_path):
    import json

    from eggopt.context import _evaluation_scope
    from eggopt.gepa.mutation import MutationRequest

    request = MutationRequest(
        ({"instruction": "parent"},),
        ({"case": "one", "feedback": "large" * 1000},),
        "Improve.",
        ({"candidate_number": 1, "aggregate_score": 0.5},),
    )
    context = {"inner_context": str(tmp_path), "outer_context": str(tmp_path)}
    with _evaluation_scope(context):
        first = request.write("eggopt.gepa.mutate.v1:abcdef0123456789ffff")
        second = request.write("eggopt.gepa.mutate.v1:abcdef0123456789ffff")

    assert first == second == tmp_path / "feedback-abcdef0123456789.json"
    assert json.loads(first.read_text()) == request.document()
    prompt = request.prompt("Improve.", first.name)
    assert first.name in prompt
    assert "large" not in prompt

    first.write_text("{}\n")
    with _evaluation_scope(context), pytest.raises(RuntimeError, match="contradicts"):
        request.write("eggopt.gepa.mutate.v1:abcdef0123456789ffff")


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
    cfg = GEPAConfig(
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


class ScriptedMutationLLM:
    current_model_key = "scripted-mutation"

    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = 0
        self.models = []

    def set_model(self, key):
        self.current_model_key = key

    def set_model_with_config(self, key, _config):
        self.current_model_key = key

    async def astream_chat(self, *_args, **_kwargs):
        self.calls += 1
        self.models.append(self.current_model_key)
        yield {
            "type": "message",
            "role": "assistant",
            "content": next(self.replies),
            "stop_reason": "end_turn",
        }


class InterruptedMutationLLM(ScriptedMutationLLM):
    async def astream_chat(self, *_args, **_kwargs):
        self.calls += 1
        self.models.append(self.current_model_key)
        if self.calls == 1:
            return
        yield {
            "type": "message",
            "role": "assistant",
            "content": next(self.replies),
            "stop_reason": "end_turn",
        }


@pytest.mark.parametrize("failed_status", ["FAILED", "RUNNING"])
def test_interrupted_mutation_recovers_same_actor_critic_interaction_on_restart(
    tmp_path, monkeypatch, failed_status
):
    import json

    from eggflow import TaskError
    from eggopt import Mutator

    monkeypatch.chdir(tmp_path)
    llm = InterruptedMutationLLM([json.dumps({"mutations": [{"instruction": "1"}]})])
    evaluator = Evaluator()
    cfg = GEPAConfig(
        run_dir=tmp_path / "mutation-recovery",
        max_candidates=1,
        max_evaluator_calls=4,
        mutation_minibatch_size=1,
        parents_per_candidate=1,
        minibatch_acceptance="improvement_or_equal",
        mutator=Mutator.eggthreads(
            llm=llm,
            identity={"model": "interrupted-mutation"},
            instruction="Improve the instruction.",
            model_key="mutation-model",
            allowed_tools=set(),
        ),
        evaluator_identity={"name": "mutation-recovery-test"},
        case_id=lambda case: case["id"],
    )
    arguments = {
        "evaluator": evaluator,
        "dataset": [{"id": "one", "target": 1}],
        "objective": "Reach the target.",
        "config": cfg,
    }

    with pytest.raises(TaskError, match="settled without a final answer"):
        optimize_anything({"instruction": "0"}, **arguments)

    if failed_status == "RUNNING":
        import sqlite3

        with sqlite3.connect(cfg.run_dir / ".egg" / "flow.db") as flow:
            flow.execute(
                "UPDATE tasks SET status='RUNNING' "
                "WHERE cache_key LIKE 'eggopt.%' AND status='FAILED'"
            )

    db = ThreadsDB(cfg.run_dir / ".egg" / "threads.sqlite")
    try:
        mutation_id = next(
            thread.thread_id for thread in list_threads(db) if thread.name == "Mutation"
        )
        original_prompt = db.conn.execute(
            "SELECT msg_id FROM events WHERE thread_id=? AND type='msg.create' "
            "AND json_extract(payload_json, '$.role')='user' "
            "AND json_extract(payload_json, '$.eggopt_actor_critic_key') IS NOT NULL",
            (mutation_id,),
        ).fetchone()[0]
    finally:
        db.close()

    result = optimize_anything({"instruction": "0"}, **arguments)

    assert result.best_candidate == {"instruction": "1"}
    assert llm.calls == 2
    assert llm.models == ["mutation-model", "mutation-model"]
    db = ThreadsDB(cfg.run_dir / ".egg" / "threads.sqlite")
    try:
        continuation = db.conn.execute(
            "SELECT payload_json FROM events WHERE thread_id=? "
            "AND type='control.interrupt' "
            "AND json_extract(payload_json, '$.purpose')='continue'",
            (mutation_id,),
        ).fetchone()
        assert continuation is not None
        assert original_prompt in continuation[0]
        prompts = db.conn.execute(
            "SELECT count(*) FROM events WHERE thread_id=? AND type='msg.create' "
            "AND json_extract(payload_json, '$.eggopt_actor_critic_key') IS NOT NULL "
            "AND json_extract(payload_json, '$.role')='user'",
            (mutation_id,),
        ).fetchone()[0]
        assert prompts == 1
    finally:
        db.close()


def test_mutation_uses_actor_critic_with_deterministic_validation(
    tmp_path, monkeypatch
):
    import json

    from eggopt import Mutator

    monkeypatch.chdir(tmp_path)
    llm = ScriptedMutationLLM(
        [
            "not json",
            json.dumps({"mutations": [{"instruction": "1"}]}),
        ]
    )
    llm.current_model_key = "prediction-model"
    evaluator = Evaluator()
    result = optimize_anything(
        {"instruction": "0"},
        evaluator=evaluator,
        dataset=[{"id": "one", "target": 1}],
        objective="Reach the target.",
        config=GEPAConfig(
            run_dir=tmp_path / "mutation",
            max_candidates=1,
            max_evaluator_calls=4,
            mutation_minibatch_size=1,
            parents_per_candidate=1,
            minibatch_acceptance="improvement_or_equal",
            mutator=Mutator.eggthreads(
                llm=llm,
                identity={"model": "scripted-mutation"},
                instruction="Improve the instruction.",
                model_key="mutation-model",
                allowed_tools=set(),
                max_correction_turns=1,
            ),
            evaluator_identity={"name": "mutation-critic-test"},
            case_id=lambda case: case["id"],
        ),
    )

    assert result.best_candidate == {"instruction": "1"}
    assert llm.calls == 2
    assert llm.models == ["mutation-model", "mutation-model"]
    db = ThreadsDB(tmp_path / "mutation" / ".egg" / "threads.sqlite")
    try:
        from eggthreads import current_thread_model

        mutation_review = db.conn.execute(
            "SELECT thread_id FROM threads WHERE name='Mutation Review'"
        ).fetchone()
        mutation = db.conn.execute(
            "SELECT thread_id FROM threads WHERE name='Mutation'"
        ).fetchone()
        validation = db.conn.execute(
            "SELECT thread_id FROM threads WHERE name='Validation'"
        ).fetchone()
        assert mutation_review and mutation and validation
        assert current_thread_model(db, mutation[0]) == "mutation-model"
        parent = db.conn.execute(
            "SELECT parent_id FROM children WHERE child_id=?", (mutation[0],)
        ).fetchone()
        assert tuple(parent) == (mutation_review[0],)
        assert not is_descendant_thread(db, mutation[0], validation[0])
    finally:
        db.conn.close()


def test_actor_critic_answer_is_bounded_by_the_next_user_turn(tmp_path):
    from eggopt.actor_critic import _answer_after_message
    from eggthreads import append_message, create_root_thread

    db = ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    actor_id = create_root_thread(db, name="Actor")
    prompt_id = append_message(
        db,
        actor_id,
        "user",
        "Produce the mutation.",
        extra={"eggopt_actor_critic_key": "turn-key"},
    )
    append_message(
        db,
        actor_id,
        "assistant",
        "",
        extra={
            "tool_calls": [
                {
                    "id": "inspect-request",
                    "type": "function",
                    "function": {"name": "inspect", "arguments": "{}"},
                }
            ]
        },
    )
    append_message(db, actor_id, "tool", "Inspection complete.")
    append_message(db, actor_id, "assistant", '{"mutations":[{"instruction":"1"}]}')
    append_message(
        db,
        actor_id,
        "user",
        "Use the `compaction-checkpoint` skill. Mode: `summary_only`.",
        extra={"compaction_summary_request": True},
    )
    append_message(db, actor_id, "assistant", "# Compaction checkpoint")

    projection = load_thread_projection(db, actor_id)
    prompt_seq = projection.message(prompt_id).created_event_seq
    assert _answer_after_message(db, actor_id, prompt_seq) == (
        '{"mutations":[{"instruction":"1"}]}'
    )
    db.close()


def test_actor_critic_open_turn_uses_its_latest_answer(tmp_path):
    from eggopt.actor_critic import _answer_after_message
    from eggthreads import append_message, create_root_thread

    db = ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    actor_id = create_root_thread(db, name="Actor")
    append_message(
        db,
        actor_id,
        "user",
        "Produce the initial mutation.",
        extra={"eggopt_actor_critic_key": "initial-key"},
    )
    append_message(db, actor_id, "assistant", "not json")
    prompt_id = append_message(
        db,
        actor_id,
        "user",
        "Revise the mutation.",
        extra={"eggopt_actor_critic_key": "revision-key"},
    )
    append_message(db, actor_id, "assistant", '{"mutations":[{"instruction":"2"}]}')

    projection = load_thread_projection(db, actor_id)
    prompt_seq = projection.message(prompt_id).created_event_seq
    assert _answer_after_message(db, actor_id, prompt_seq) == (
        '{"mutations":[{"instruction":"2"}]}'
    )
    db.close()


def test_mutation_replay_keeps_answer_before_compaction_checkpoint(
    tmp_path, monkeypatch
):
    import json
    import pickle

    from eggflow import TaskError
    from eggopt import ActorCriticResult, Mutator
    from eggthreads import append_message

    monkeypatch.chdir(tmp_path)
    mutation = json.dumps({"mutations": [{"instruction": "1"}]})
    llm = ScriptedMutationLLM([mutation])
    evaluator = Evaluator()
    cfg = GEPAConfig(
        run_dir=tmp_path / "mutation-compaction-replay",
        max_candidates=1,
        max_evaluator_calls=4,
        mutation_minibatch_size=1,
        parents_per_candidate=1,
        minibatch_acceptance="improvement_or_equal",
        mutator=Mutator.eggthreads(
            llm=llm,
            identity={"model": "mutation-compaction-replay"},
            instruction="Improve the instruction.",
            model_key="mutation-model",
            allowed_tools=set(),
        ),
        evaluator_identity={"name": "mutation-compaction-replay"},
        case_id=lambda case: case["id"],
    )
    arguments = {
        "evaluator": evaluator,
        "dataset": [{"id": "one", "target": 1}],
        "objective": "Reach the target.",
        "config": cfg,
    }

    first = optimize_anything({"instruction": "0"}, **arguments)
    assert first.best_candidate == {"instruction": "1"}

    db = ThreadsDB(cfg.run_dir / ".egg" / "threads.sqlite")
    mutation_id = next(
        thread.thread_id for thread in list_threads(db) if thread.name == "Mutation"
    )
    append_message(
        db,
        mutation_id,
        "user",
        "Use the `compaction-checkpoint` skill. Mode: `summary_only`.",
        extra={"compaction_summary_request": True},
    )
    append_message(db, mutation_id, "assistant", "# Compaction checkpoint")
    db.close()

    from eggflow import Result, TaskStore

    flow = TaskStore(str(cfg.run_dir / ".egg" / "flow.db"))
    actor_row = flow.conn.execute(
        "SELECT cache_key, result_blob FROM tasks "
        "WHERE cache_key LIKE 'eggopt.actor-critic.v2:%'"
    ).fetchone()
    actor_result = pickle.loads(actor_row["result_blob"]).value
    legacy_key = actor_row["cache_key"].replace(
        "eggopt.actor-critic.v2:", "eggopt.actor-critic.v1:", 1
    )
    legacy_result = replace(actor_result, answer="# Compaction checkpoint")
    flow.conn.execute(
        "INSERT OR REPLACE INTO tasks(cache_key, status, result_blob) VALUES (?, ?, ?)",
        (legacy_key, "COMPLETED", pickle.dumps(Result(value=legacy_result))),
    )
    flow.conn.commit()
    for row in flow.conn.execute("SELECT cache_key FROM tasks"):
        key = row["cache_key"]
        if key.startswith(("eggopt.actor-critic.v2:", "eggopt.gepa.mutate.v")):
            flow.update(key, "FAILED")
    assert flow.get(legacy_key)["status"] == "COMPLETED"
    assert isinstance(legacy_result, ActorCriticResult)
    flow.close()

    replay_llm = ScriptedMutationLLM([])
    arguments["config"] = replace(
        cfg,
        mutator=Mutator.eggthreads(
            llm=replay_llm,
            identity={"model": "mutation-compaction-replay"},
            instruction="Improve the instruction.",
            model_key="mutation-model",
            allowed_tools=set(),
        ),
    )
    try:
        replay = optimize_anything({"instruction": "0"}, **arguments)
    except TaskError as error:  # pragma: no cover - gives the regression clear context
        raise AssertionError(str(error)) from error

    assert replay.best_candidate == {"instruction": "1"}
    assert replay_llm.calls == 0
