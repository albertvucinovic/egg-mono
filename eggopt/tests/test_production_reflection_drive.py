from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from eggflow import FlowExecutor, TaskStore
from eggthreads import (
    RunnerConfig,
    ThreadRunner,
    ThreadsDB,
    ToolRegistry,
    create_child_thread,
    enqueue_user_tool_call,
    get_thread_sandbox_config,
    get_thread_tools_config,
    list_children_with_meta,
    load_thread_projection,
    set_thread_tool_allowlist,
)

from eggopt.gepa import (
    EggthreadsReflectionDrive,
    EggthreadsReflectionLM,
    SOLVER_SAFE_PROFILE_NAME,
    SOLVER_SAFE_PROFILE_VERSION,
    SOLVER_SAFE_TOOLS,
    configure_solver_safe_tools,
    create_solver_safe_study,
)


class ScriptedToolLLM:
    current_model_key = "scripted-model"

    def __init__(self) -> None:
        self.calls = 0
        self.tool_names: list[set[str]] = []
        self.messages: list[list[dict]] = []

    def set_model(self, model_key):
        self.current_model_key = model_key

    def set_model_with_config(self, model_key, config):
        del config
        self.current_model_key = model_key

    async def astream_chat(
        self,
        messages,
        tools=None,
        tool_choice=None,
        timeout=None,
        **kwargs,
    ):
        del tool_choice, timeout, kwargs
        self.calls += 1
        self.messages.append(messages)
        self.tool_names.append(
            {item["function"]["name"] for item in (tools or [])}
        )
        if self.calls in {1, 3}:
            call_id = f"python-{self.calls}"
            arguments = json.dumps({"value": f"turn-{self.calls}"})
            tool_call = {
                "id": call_id,
                "type": "function",
                "function": {"name": "python_exec", "arguments": arguments},
            }
            yield {"type": "tool_calls_delta", "delta": [tool_call]}
            yield {
                "type": "message",
                "role": "assistant",
                "content": "",
                "tool_calls": [tool_call],
                "stop_reason": "tool_calls",
            }
            return
        instruction = "child" if self.calls == 2 else "grandchild"
        yield {
            "type": "message",
            "role": "assistant",
            "content": json.dumps(
                {"mutations": [{"instruction": instruction}]}
            ),
            "stop_reason": "end_turn",
        }


class MustNotRunLLM:
    current_model_key = "scripted-model"

    def __init__(self) -> None:
        self.calls = 0

    async def astream_chat(self, *args, **kwargs):
        del args, kwargs
        self.calls += 1
        raise AssertionError("semantic reflection cache was not reused")
        yield


def _db(tmp_path: Path) -> ThreadsDB:
    db = ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    return db


def _registry(executions: list[str], blocked: list[str]) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        "python_exec",
        "Deterministic test calculation",
        {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
        lambda args: executions.append(args["value"]) or f"result:{args['value']}",
    )
    registry.register(
        "web_search",
        "Must remain unavailable",
        {"type": "object", "properties": {}},
        lambda args: blocked.append("executed") or "blocked",
    )
    return registry


def _reflector(
    tmp_path: Path,
    db: ThreadsDB,
    study_id: str,
    llm,
    registry: ToolRegistry,
    identity: dict,
    **drive_options,
) -> EggthreadsReflectionLM:
    drive = EggthreadsReflectionDrive(
        llm=llm,
        tools=registry,
        drive_identity=identity,
        runner_config=RunnerConfig(tool_timeout_sec=30),
        auto_approve_tools=True,
        **drive_options,
    )
    return EggthreadsReflectionLM(
        FlowExecutor(TaskStore(str(tmp_path / "flow.sqlite"))),
        db,
        drive=drive,
        reflector_id="production-reflector",
        reflector_version="1",
        reflector_config={"schema": "strict-mutations-v1"},
        study_thread_id=study_id,
    )


class RepairingLLM:
    current_model_key = "scripted-model"

    def __init__(self, responses: list[str]) -> None:
        self.responses = iter(responses)
        self.messages: list[list[dict]] = []

    async def astream_chat(self, messages, **kwargs):
        del kwargs
        self.messages.append(messages)
        yield {
            "type": "message",
            "role": "assistant",
            "content": next(self.responses),
            "stop_reason": "end_turn",
        }


class StreamingCeilingLLM:
    current_model_key = "scripted-model"

    def __init__(self) -> None:
        self.cancelled = False

    async def astream_chat(self, messages, **kwargs):
        del messages, kwargs
        try:
            while True:
                yield {"type": "content_delta", "text": "token " * 8}
                await asyncio.sleep(0)
        finally:
            self.cancelled = True


def test_malformed_envelope_repairs_in_same_mutation_thread(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = _db(tmp_path)
    study_id, profile = create_solver_safe_study(db, workspace=tmp_path / "workspace")
    llm = RepairingLLM(
        [
            "not json and secret transcript details",
            json.dumps({"mutations": [{"instruction": "fixed"}]}),
        ]
    )
    reflector = _reflector(
        tmp_path,
        db,
        study_id,
        llm,
        _registry([], []),
        {"model": "repair-test", "profile": profile},
        max_correction_turns=2,
    )
    dataset = {"instruction": [{"Feedback": "repair"}]}

    proposal, _next = reflector.reflect(
        {"instruction": "seed"}, dataset, ["instruction"]
    )
    assert proposal.new_texts == {"instruction": "fixed"}
    occurrence = reflector.occurrence({"instruction": "seed"}, dataset, ["instruction"])
    assert occurrence is not None
    assert len(llm.messages) == 2
    transcript = load_thread_projection(
        db,
        occurrence.mutation_thread_id,
        db.max_event_seq(occurrence.mutation_thread_id),
    ).messages
    repairs = [
        message
        for message in transcript
        if message.payload.get("eggopt_kind") == "eggopt.gepa.reflection-repair.v1"
    ]
    malformed = [
        message
        for message in transcript
        if message.payload.get("role") == "assistant"
        and message.payload.get("content") == "not json and secret transcript details"
    ]
    final = [
        message
        for message in transcript
        if message.payload.get("eggopt_kind") == "eggopt.gepa.reflection-response.v1"
    ]
    assert len(malformed) == 1
    assert len(repairs) == 1
    assert len(final) == 1
    assert malformed[0].created_event_seq < repairs[0].created_event_seq < final[0].created_event_seq
    assert repairs[0].payload["correction_turn"] == 1
    assert "strict JSON" in repairs[0].payload["content"]
    assert repairs[0].payload["validation_feedback"] == repairs[0].payload["content"]
    assert "secret transcript details" not in repairs[0].payload["content"]
    assert len(list_children_with_meta(db, occurrence.iteration_thread_id)) == 1

    changed = _reflector(
        tmp_path,
        db,
        study_id,
        RepairingLLM([json.dumps({"mutations": [{"instruction": "other"}]})]),
        _registry([], []),
        {"model": "repair-test", "profile": profile},
        max_correction_turns=1,
    )
    assert changed.semantic_key(
        {"instruction": "seed"}, dataset, ["instruction"]
    ) != reflector.semantic_key({"instruction": "seed"}, dataset, ["instruction"])
    identity = reflector.drive.semantic_identity["mutation_repair"]
    assert identity == {
        "policy": "eggopt.gepa.strict-mutation-repair",
        "version": "1",
        "max_correction_turns": 2,
    }


def test_malformed_envelope_stops_after_configured_repairs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = _db(tmp_path)
    study_id, profile = create_solver_safe_study(db, workspace=tmp_path / "workspace")
    llm = RepairingLLM(["bad", "still bad", "must not run"])
    reflector = _reflector(
        tmp_path,
        db,
        study_id,
        llm,
        _registry([], []),
        {"model": "repair-limit", "profile": profile},
        max_correction_turns=1,
    )

    with pytest.raises(Exception, match="remained invalid after 1 corrective turn"):
        reflector(
            {"instruction": "seed"},
            {"instruction": [{"Feedback": "repair"}]},
            ["instruction"],
        )
    assert len(llm.messages) == 2


def test_context_ceiling_rejects_before_provider_call(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = _db(tmp_path)
    study_id, profile = create_solver_safe_study(db, workspace=tmp_path / "workspace")
    reflector = _reflector(
        tmp_path,
        db,
        study_id,
        MustNotRunLLM(),
        _registry([], []),
        {"model": "preflight-ceiling", "profile": profile},
        context_ceiling_tokens=1,
    )

    with pytest.raises(Exception, match="context ceiling reached before provider call"):
        reflector(
            {"instruction": "seed"},
            {"instruction": [{"Feedback": "stream"}]},
            ["instruction"],
        )
    assert reflector.drive.llm.calls == 0


def test_streaming_context_ceiling_interrupts_only_reflection_operation(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    db = _db(tmp_path)
    study_id, profile = create_solver_safe_study(db, workspace=tmp_path / "workspace")
    llm = StreamingCeilingLLM()
    reflector = _reflector(
        tmp_path,
        db,
        study_id,
        llm,
        _registry([], []),
        {"model": "ceiling-test", "profile": profile},
        context_ceiling_tokens=180,
    )
    from eggthreads import provider_context_token_stats

    # The inherited Mutation context is the operation whose live stream is bounded.
    assert provider_context_token_stats(db, study_id)["context_tokens"] < 180

    with pytest.raises(Exception, match="context ceiling reached"):
        reflector(
            {"instruction": "seed"},
            {"instruction": [{"Feedback": "stream"}]},
            ["instruction"],
        )
    occurrence = reflector.occurrence(
        {"instruction": "seed"},
        {"instruction": [{"Feedback": "stream"}]},
        ["instruction"],
    )
    assert occurrence is not None
    assert llm.cancelled is True
    assert db.current_open(occurrence.mutation_thread_id) is None
    assert db.get_thread(study_id).status == "active"
    assert db.get_thread(occurrence.mutation_thread_id).status == "active"
    events = list(db.events_since(occurrence.mutation_thread_id, 0))
    assert any(event["type"] == "control.interrupt" for event in events)
    assert reflector.drive.semantic_identity["context_ceiling"] == {
        "policy": "eggopt.gepa.streaming-context-ceiling",
        "version": "1",
        "max_tokens": 180,
    }


def test_production_drive_validates_repair_and_ceiling_options():
    registry = _registry([], [])
    for options, message in [
        ({"max_correction_turns": -1}, "max_correction_turns"),
        ({"max_correction_turns": True}, "max_correction_turns"),
        ({"context_ceiling_tokens": 0}, "context_ceiling_tokens"),
        ({"context_ceiling_tokens": True}, "context_ceiling_tokens"),
    ]:
        with pytest.raises(ValueError, match=message):
            EggthreadsReflectionDrive(
                llm=MustNotRunLLM(),
                tools=registry,
                drive_identity={"model": "validation-test"},
                **options,
            )
    with pytest.raises(ValueError, match="reserved"):
        EggthreadsReflectionDrive(
            llm=MustNotRunLLM(),
            tools=registry,
            drive_identity={"mutation_repair": "caller override"},
        )


def test_solver_safe_profile_is_exact_sandboxed_and_inherited(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = _db(tmp_path)
    workspace = tmp_path / "workspace"
    study_id, identity = create_solver_safe_study(db, workspace=workspace)

    assert identity["profile"] == SOLVER_SAFE_PROFILE_NAME
    assert identity["version"] == SOLVER_SAFE_PROFILE_VERSION
    assert set(identity["tools"]) == SOLVER_SAFE_TOOLS
    assert identity["sandbox"]["network"]["allowedDomains"] == []
    root_tools = get_thread_tools_config(db, study_id)
    assert root_tools.allowed_tools == SOLVER_SAFE_TOOLS

    child = create_child_thread(db, study_id, name="Mutation")
    assert get_thread_tools_config(db, child).allowed_tools == SOLVER_SAFE_TOOLS
    set_thread_tool_allowlist(db, study_id, {"python_exec", "bash"})
    set_thread_tool_allowlist(db, child, set(SOLVER_SAFE_TOOLS))
    assert get_thread_tools_config(db, child).allowed_tools == {"python_exec", "bash"}

    sandbox = get_thread_sandbox_config(db, child)
    assert sandbox.enabled is True
    assert sandbox.provider == "docker"
    assert sandbox.settings["network"]["allowedDomains"] == []
    assert sandbox.settings["workspace"] == "/workspace"
    assert sandbox.settings["filesystem"]["allowWrite"] == ["."]
    assert ".egg" in sandbox.settings["filesystem"]["denyWrite"]
    assert sandbox.user_control_enabled is False


def test_production_drive_tool_round_trip_policy_affinity_and_cache(
    tmp_path,
    monkeypatch,
):
    monkeypatch.chdir(tmp_path)
    db = _db(tmp_path)
    study_id, profile = create_solver_safe_study(
        db, workspace=tmp_path / "workspace"
    )
    executions: list[str] = []
    blocked: list[str] = []
    llm = ScriptedToolLLM()
    identity = {
        "model": "scripted-model-v1",
        "tool_behavior": "test-registry-v1",
        "profile": profile,
    }
    reflector = _reflector(
        tmp_path, db, study_id, llm, _registry(executions, blocked), identity
    )
    dataset = {"instruction": [{"Feedback": "use the calculation"}]}

    proposal, _next = reflector.reflect(
        {"instruction": "seed"}, dataset, ["instruction"]
    )
    assert proposal.new_texts == {"instruction": "child"}
    occurrence = reflector.occurrence(
        {"instruction": "seed"}, dataset, ["instruction"]
    )
    assert occurrence is not None
    assert executions == ["turn-1"]
    assert blocked == []
    assert all(names == {"python_exec"} for names in llm.tool_names)
    child_tools = get_thread_tools_config(db, occurrence.mutation_thread_id)
    assert child_tools.allowed_tools == SOLVER_SAFE_TOOLS
    assert not child_tools.is_tool_allowed("web_search")

    followup, _next = reflector.reflect(
        {"instruction": "child"},
        {"instruction": [{"Feedback": "refine after tool evidence"}]},
        ["instruction"],
    )
    assert followup.new_texts == {"instruction": "grandchild"}
    followup_occurrence = reflector.occurrence(
        {"instruction": "child"},
        {"instruction": [{"Feedback": "refine after tool evidence"}]},
        ["instruction"],
    )
    assert followup_occurrence is not None
    assert followup_occurrence.mutation_thread_id == occurrence.mutation_thread_id
    assert executions == ["turn-1", "turn-3"]
    assert any(
        message.get("role") == "tool" and message.get("content") == "result:turn-1"
        for message in llm.messages[2]
    )

    transcript = load_thread_projection(
        db,
        occurrence.mutation_thread_id,
        db.max_event_seq(occurrence.mutation_thread_id),
    ).messages
    assert sum(message.payload.get("role") == "tool" for message in transcript) == 2
    assert all(
        get_thread_tools_config(db, occurrence.mutation_thread_id).is_tool_allowed(name)
        for name in llm.tool_names[-1]
    )

    fresh_llm = MustNotRunLLM()
    fresh = _reflector(
        tmp_path,
        db,
        study_id,
        fresh_llm,
        _registry([], []),
        identity,
    )
    replay, _next = fresh.reflect(
        {"instruction": "seed"}, dataset, ["instruction"]
    )
    assert replay.new_texts == {"instruction": "child"}
    assert fresh_llm.calls == 0

    changed_llm = ScriptedToolLLM()
    changed = _reflector(
        tmp_path,
        db,
        study_id,
        changed_llm,
        _registry([], []),
        {**identity, "model": "scripted-model-v2"},
    )
    changed_proposal, _next = changed.reflect(
        {"instruction": "seed"}, dataset, ["instruction"]
    )
    assert changed_proposal.new_texts == {"instruction": "child"}
    assert changed_llm.calls == 2


def test_disallowed_registered_tool_is_hidden_and_denied(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = _db(tmp_path)
    study_id, _profile = create_solver_safe_study(
        db, workspace=tmp_path / "workspace"
    )
    child = create_child_thread(db, study_id, name="Mutation")
    blocked: list[str] = []
    registry = _registry([], blocked)

    tool_call_id = enqueue_user_tool_call(
        db,
        child,
        "web_search",
        {},
        content="web_search()",
        hidden=True,
        auto_approve=True,
        approval_reason="test denial path",
    )
    runner = ThreadRunner(db, child, llm=object(), tools=registry)
    assert __import__("asyncio").run(runner.run_once()) is True
    assert __import__("asyncio").run(runner.run_once()) is True
    assert blocked == []
    row = db.conn.execute(
        "SELECT payload_json FROM events WHERE thread_id=? AND type='msg.create' "
        "AND json_extract(payload_json, '$.tool_call_id')=?",
        (child, tool_call_id),
    ).fetchone()
    assert row is not None
    assert "not allowed" in json.loads(row[0])["content"]


def test_production_drive_requires_explicit_safe_study(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = _db(tmp_path)
    registry = _registry([], [])
    drive = EggthreadsReflectionDrive(
        llm=MustNotRunLLM(),
        tools=registry,
        drive_identity={"model": "scripted-model-v1"},
    )
    with pytest.raises(ValueError, match="explicit study_thread_id"):
        EggthreadsReflectionLM(
            FlowExecutor(TaskStore(str(tmp_path / "flow.sqlite"))),
            db,
            drive=drive,
            reflector_id="production-reflector",
            reflector_version="1",
            reflector_config={},
        )

    root, _profile = create_solver_safe_study(
        db, workspace=tmp_path / "workspace"
    )
    child = create_child_thread(db, root, name="not-a-root")
    with pytest.raises(ValueError, match="study root"):
        configure_solver_safe_tools(
            db, child, workspace=tmp_path / "workspace"
        )
