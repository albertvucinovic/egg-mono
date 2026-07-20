from __future__ import annotations

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
                "function": {"name": "python", "arguments": arguments},
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
        "python",
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
) -> EggthreadsReflectionLM:
    drive = EggthreadsReflectionDrive(
        llm=llm,
        tools=registry,
        drive_identity=identity,
        runner_config=RunnerConfig(tool_timeout_sec=30),
        auto_approve_tools=True,
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
    set_thread_tool_allowlist(db, study_id, {"python", "bash"})
    set_thread_tool_allowlist(db, child, set(SOLVER_SAFE_TOOLS))
    assert get_thread_tools_config(db, child).allowed_tools == {"python", "bash"}

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
    assert all(names == {"python"} for names in llm.tool_names)
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
