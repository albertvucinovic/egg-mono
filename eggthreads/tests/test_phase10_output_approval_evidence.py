from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import eggthreads as ts
from eggthreads.runner import ThreadRunner
from eggthreads.tools import ToolRegistry


@pytest.fixture(autouse=True)
def _reset_process_output_recovery_budget():
    """Keep literal process-local retry tests isolated from one another."""

    from eggthreads import runner as runner_module

    runner_module._PROCESS_TOOL_OUTPUT_RECOVERY_RETRIES.reset_for_tests()
    yield
    runner_module._PROCESS_TOOL_OUTPUT_RECOVERY_RETRIES.reset_for_tests()


def _event_rows(db: ts.ThreadsDB, thread_id: str) -> list[tuple[str, dict, str | None]]:
    rows = db.conn.execute(
        "SELECT type, payload_json, invoke_id FROM events WHERE thread_id=? ORDER BY event_seq",
        (thread_id,),
    ).fetchall()
    return [(str(row[0]), json.loads(row[1]), row[2]) for row in rows]


def _payloads(db: ts.ThreadsDB, thread_id: str, event_type: str) -> list[dict]:
    return [
        payload
        for type_, payload, _invoke_id in _event_rows(db, thread_id)
        if type_ == event_type
    ]


def _registry(output: str) -> ToolRegistry:
    tools = ToolRegistry()
    tools.register(
        "evidence_output",
        "Phase 10 output evidence",
        {"type": "object", "properties": {}},
        lambda _args: output,
    )
    return tools


def _enqueue(db: ts.ThreadsDB, thread_id: str, *, tool_call_id: str) -> None:
    ts.enqueue_user_tool_call(
        db,
        thread_id,
        "evidence_output",
        {},
        content="$ evidence-output",
        hidden=False,
        tool_call_id=tool_call_id,
    )


@pytest.mark.parametrize(
    ("output", "expected_decision", "expects_artifact"),
    [
        pytest.param("ordinary successful output", "whole", False, id="below-threshold"),
        pytest.param("x" * 120_000, "partial", True, id="above-threshold"),
    ],
)
def test_phase10_healthy_runner_automatically_decides_and_publishes_output(
    tmp_path, monkeypatch, output, expected_decision, expects_artifact
) -> None:
    """Characterize the healthy TC4 -> TC5 -> TC6 automatic path."""

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("EGG_OUTPUT_OPTIMIZER", raising=False)
    db = ts.ThreadsDB(tmp_path / "healthy.sqlite")
    db.init_schema()
    thread_id = ts.create_root_thread(db, name="phase10 healthy")
    tool_call_id = "phase10-healthy-call"
    _enqueue(db, thread_id, tool_call_id=tool_call_id)
    runner = ThreadRunner(db, thread_id, llm=object(), tools=_registry(output))

    assert asyncio.run(runner.run_once()) is True
    after_execution = ts.build_tool_call_states(db, thread_id)[tool_call_id]
    assert after_execution.state == "TC5"
    assert after_execution.finished_reason == "success"
    assert after_execution.finished_output == output
    approval = _payloads(db, thread_id, "tool_call.output_approval")
    assert len(approval) == 1
    assert approval[0]["decision_source"] == "automatic_policy"
    assert approval[0]["decision"] == expected_decision
    assert bool(approval[0]["artifact_path"]) is expects_artifact
    assert ("read_long_tool_output(" in approval[0]["preview"]) is expects_artifact

    assert asyncio.run(runner.run_once()) is True
    final = ts.build_tool_call_states(db, thread_id)[tool_call_id]
    assert final.state == "TC6"
    tool_messages = [
        payload
        for payload in _payloads(db, thread_id, "msg.create")
        if payload.get("role") == "tool" and payload.get("tool_call_id") == tool_call_id
    ]
    assert len(tool_messages) == 1
    assert ("read_long_tool_output(" in tool_messages[0]["content"]) is expects_artifact


def test_phase10_policy_failure_retries_after_restart_and_second_scheduler(
    tmp_path, monkeypatch
) -> None:
    """Recover the durable post-finish/pre-decision failure window exactly once."""

    db = ts.ThreadsDB(tmp_path / "stranded.sqlite")
    db.init_schema()
    thread_id = ts.create_root_thread(db, name="phase10 stranded")
    tool_call_id = "phase10-stranded-call"
    output = "successful output awaiting automatic publication"
    _enqueue(db, thread_id, tool_call_id=tool_call_id)

    from eggthreads import output_policy

    real_decide = output_policy.decide_output_publication

    def fail_policy(*_args, **_kwargs):
        raise RuntimeError("phase10 injected policy failure")

    monkeypatch.setattr("eggthreads.output_policy.decide_output_publication", fail_policy)
    first_runner = ThreadRunner(
        db,
        thread_id,
        llm=object(),
        tools=_registry(output),
        owner="egg",
    )

    assert asyncio.run(first_runner.run_once()) is True
    stranded = ts.build_tool_call_states(db, thread_id)[tool_call_id]
    assert stranded.state == "TC4"
    assert stranded.finished_reason == "success"
    assert stranded.finished_output == output
    assert stranded.owner_invoke_id
    assert stranded.finished_event_seq is not None
    assert ts.thread_state(db, thread_id) == "running"
    recovery = ts.discover_runner_actionable(db, thread_id)
    assert recovery is not None
    assert recovery.recovery_mode == "stranded_successful_tc4"
    assert db.current_open(thread_id) is None
    assert len(_payloads(db, thread_id, "tool_call.finished")) == 1
    assert _payloads(db, thread_id, "tool_call.output_approval") == []

    # Model process restart plus a competing EggW-owned scheduler connection.
    # Hold the winner after lease acquisition but before finalization so the
    # loser deterministically observes that live recovery fence.
    policy_entered = threading.Event()
    release_policy = threading.Event()

    def gated_decide(*args, **kwargs):
        policy_entered.set()
        assert release_policy.wait(timeout=2)
        return real_decide(*args, **kwargs)

    monkeypatch.setattr(
        "eggthreads.output_policy.decide_output_publication",
        gated_decide,
    )
    winner_result: list[bool] = []
    winner_errors: list[BaseException] = []

    def run_winner() -> None:
        local = ts.ThreadsDB(db.path)
        try:
            runner = ThreadRunner(
                local,
                thread_id,
                llm=object(),
                tools=_registry(output),
                owner="egg-restart",
                manual_output_recovery=True,
            )
            winner_result.append(asyncio.run(runner.run_once()))
        except BaseException as exc:  # pragma: no cover - asserted below
            winner_errors.append(exc)
        finally:
            local.conn.close()

    winner = threading.Thread(target=run_winner)
    winner.start()
    assert policy_entered.wait(timeout=2)

    eggw_db = ts.ThreadsDB(db.path)
    try:
        eggw_runner = ThreadRunner(
            eggw_db,
            thread_id,
            llm=object(),
            tools=_registry(output),
            owner="eggw",
        )
        assert asyncio.run(eggw_runner.run_once()) is False
    finally:
        eggw_db.conn.close()
        release_policy.set()
        winner.join(timeout=5)

    assert winner_errors == []
    assert winner_result == [True]

    recovered = ts.build_tool_call_states(db, thread_id)[tool_call_id]
    assert recovered.state == "TC6"
    assert recovered.finished_event_seq == stranded.finished_event_seq
    assert len(_payloads(db, thread_id, "tool_call.finished")) == 1
    approvals = _payloads(db, thread_id, "tool_call.output_approval")
    assert len(approvals) == 1
    assert approvals[0]["decision_source"] == "automatic_policy"
    tool_messages = [
        payload
        for payload in _payloads(db, thread_id, "msg.create")
        if payload.get("role") == "tool"
        and payload.get("tool_call_id") == tool_call_id
    ]
    assert len(tool_messages) == 1



def test_phase10_live_lease_suppresses_successful_tc4_recovery(tmp_path) -> None:
    """The original live owner retains authority until its lease ends."""

    db = ts.ThreadsDB(tmp_path / "live-owner.sqlite")
    db.init_schema()
    thread_id = ts.create_root_thread(db, name="phase10 live owner")
    tool_call_id = "phase10-live-owner-call"
    _enqueue(db, thread_id, tool_call_id=tool_call_id)
    owner = "phase10-live-owner"
    assert db.try_open_stream(
        thread_id,
        owner,
        "2999-01-01 00:00:00",
        owner="egg",
        purpose="tool",
    )
    db.append_event(
        "phase10-live-start",
        thread_id,
        "tool_call.execution_started",
        {"tool_call_id": tool_call_id},
        invoke_id=owner,
    )
    db.append_event(
        "phase10-live-finish",
        thread_id,
        "tool_call.finished",
        {
            "tool_call_id": tool_call_id,
            "reason": "success",
            "output": "owner still finalizing",
        },
        invoke_id=owner,
    )

    assert ts.discover_runner_actionable(db, thread_id) is None
    assert asyncio.run(
        ThreadRunner(
            db,
            thread_id,
            llm=object(),
            owner="eggw",
        ).run_once()
    ) is False
    assert ts.build_tool_call_states(db, thread_id)[tool_call_id].state == "TC4"
    assert _payloads(db, thread_id, "tool_call.output_approval") == []

def test_phase10_expired_owner_lease_is_recovered_without_reexecution(
    tmp_path,
) -> None:
    """A fresh owner applies policy to successful TC4 without rerunning its tool."""

    db = ts.ThreadsDB(tmp_path / "expired.sqlite")
    db.init_schema()
    thread_id = ts.create_root_thread(db, name="phase10 expired")
    tool_call_id = "phase10-expired-call"
    output = "finished before owner disappeared"
    _enqueue(db, thread_id, tool_call_id=tool_call_id)
    old_invoke_id = "phase10-old-owner"
    expired = (datetime.now(timezone.utc) - timedelta(minutes=1)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    assert db.try_open_stream(
        thread_id,
        old_invoke_id,
        expired,
        owner="egg",
        purpose="tool",
    )
    writer = db.invocation_writer(thread_id, old_invoke_id)
    db.append_event(
        "phase10-finished-before-crash",
        thread_id,
        "tool_call.execution_started",
        {"tool_call_id": tool_call_id},
        invoke_id=old_invoke_id,
    )
    db.append_event(
        "phase10-durable-finish",
        thread_id,
        "tool_call.finished",
        {"tool_call_id": tool_call_id, "reason": "success", "output": output},
        invoke_id=old_invoke_id,
    )

    with pytest.raises(ts.LeaseLost):
        ts.finalize_tool_output(
            db,
            thread_id,
            tool_call_id,
            decision="whole",
            source="automatic_policy",
            expected_event_seq=ts.build_tool_call_states(db, thread_id)[
                tool_call_id
            ].state_event_seq,
            invocation_writer=writer,
        )
    recovery = ts.discover_runner_actionable(db, thread_id)
    assert recovery is not None
    assert recovery.recovery_mode == "stranded_successful_tc4"

    executions = 0

    def must_not_run(_args):
        nonlocal executions
        executions += 1
        return "duplicate side effect"

    tools = ToolRegistry()
    tools.register(
        "evidence_output",
        "Phase 10 output evidence",
        {"type": "object", "properties": {}},
        must_not_run,
    )
    contender = ts.ThreadsDB(db.path)
    try:
        contender_runner = ThreadRunner(
            contender,
            thread_id,
            llm=object(),
            tools=tools,
            owner="eggw",
        )
        assert asyncio.run(contender_runner.run_once()) is True
    finally:
        contender.conn.close()

    final = ts.build_tool_call_states(db, thread_id)[tool_call_id]
    assert executions == 0
    assert final.state == "TC6"
    assert final.finished_reason == "success"
    assert final.finished_output == output
    assert len(_payloads(db, thread_id, "tool_call.finished")) == 1
    assert len(_payloads(db, thread_id, "tool_call.output_approval")) == 1
    interrupts = _payloads(db, thread_id, "control.interrupt")
    assert [payload["reason"] for payload in interrupts] == ["expired_lease_takeover"]




def test_phase10_read_only_runner_recovers_without_invoking_assistant_tool(
    tmp_path,
) -> None:
    """NO_API_CALLS permits publication recovery but not new RA2 execution."""

    db = ts.ThreadsDB(tmp_path / "read-only.sqlite")
    db.init_schema()
    thread_id = ts.create_root_thread(db, name="phase10 read only")
    tool_call_id = "phase10-read-only-call"
    ts.append_message(
        db,
        thread_id,
        "assistant",
        "",
        extra={
            "tool_calls": [
                {
                    "id": tool_call_id,
                    "type": "function",
                    "function": {
                        "name": "evidence_output",
                        "arguments": "{}",
                    },
                }
            ]
        },
    )
    db.append_event(
        "phase10-read-only-approval",
        thread_id,
        "tool_call.approval",
        {"tool_call_id": tool_call_id, "decision": "granted"},
    )
    db.append_event(
        "phase10-read-only-start",
        thread_id,
        "tool_call.execution_started",
        {"tool_call_id": tool_call_id},
        invoke_id="phase10-read-only-old",
    )
    db.append_event(
        "phase10-read-only-finish",
        thread_id,
        "tool_call.finished",
        {
            "tool_call_id": tool_call_id,
            "reason": "success",
            "output": "already finished",
        },
        invoke_id="phase10-read-only-old",
    )

    executions = 0

    def must_not_run(_args):
        nonlocal executions
        executions += 1
        return "duplicate side effect"

    tools = ToolRegistry()
    tools.register(
        "evidence_output",
        "Phase 10 output evidence",
        {"type": "object", "properties": {}},
        must_not_run,
    )
    runner = ThreadRunner(
        db,
        thread_id,
        llm=object(),
        tools=tools,
        config=ts.RunnerConfig(no_api_calls=True),
    )

    assert asyncio.run(runner.run_once()) is True
    assert executions == 0
    assert ts.build_tool_call_states(db, thread_id)[tool_call_id].state == "TC6"


def test_phase10_multi_tool_recovery_keeps_parent_order_and_skips_reexecution(
    tmp_path,
) -> None:
    """Recover one parent batch in declaration order without rerunning tools."""

    db = ts.ThreadsDB(tmp_path / "multi-tool.sqlite")
    db.init_schema()
    thread_id = ts.create_root_thread(db, name="phase10 multi tool")
    call_ids = ["phase10-multi-a", "phase10-multi-b"]
    ts.append_message(
        db,
        thread_id,
        "assistant",
        "",
        extra={
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": "evidence_output",
                        "arguments": "{}",
                    },
                }
                for call_id in call_ids
            ]
        },
    )
    for index, call_id in enumerate(call_ids):
        db.append_event(
            f"phase10-multi-approve-{index}",
            thread_id,
            "tool_call.approval",
            {"tool_call_id": call_id, "decision": "granted"},
        )
        db.append_event(
            f"phase10-multi-start-{index}",
            thread_id,
            "tool_call.execution_started",
            {"tool_call_id": call_id},
            invoke_id="phase10-multi-old-owner",
        )
        db.append_event(
            f"phase10-multi-finish-{index}",
            thread_id,
            "tool_call.finished",
            {
                "tool_call_id": call_id,
                "reason": "success",
                "output": f"result {index}",
            },
            invoke_id="phase10-multi-old-owner",
        )

    executions = 0

    def must_not_run(_args):
        nonlocal executions
        executions += 1
        return "duplicate side effect"

    tools = ToolRegistry()
    tools.register(
        "evidence_output",
        "Phase 10 output evidence",
        {"type": "object", "properties": {}},
        must_not_run,
    )
    assert asyncio.run(
        ThreadRunner(db, thread_id, llm=object(), tools=tools).run_once()
    ) is True

    assert executions == 0
    states = ts.build_tool_call_states(db, thread_id)
    assert [states[call_id].state for call_id in call_ids] == ["TC6", "TC6"]
    approvals = _payloads(db, thread_id, "tool_call.output_approval")
    assert [payload["tool_call_id"] for payload in approvals] == call_ids
    tool_messages = [
        payload
        for payload in _payloads(db, thread_id, "msg.create")
        if payload.get("role") == "tool"
    ]
    assert [payload["tool_call_id"] for payload in tool_messages] == call_ids
    assert len(_payloads(db, thread_id, "tool_call.finished")) == 2


def test_phase10_long_output_artifact_failure_retries_from_raw_tc4(
    tmp_path, monkeypatch
) -> None:
    """A failed artifact plan leaves raw TC4 durable for a later recovery pass."""

    monkeypatch.chdir(tmp_path)
    db = ts.ThreadsDB(tmp_path / "artifact-retry.sqlite")
    db.init_schema()
    thread_id = ts.create_root_thread(db, name="phase10 artifact retry")
    tool_call_id = "phase10-artifact-retry-call"
    output = "r" * 120_000
    _enqueue(db, thread_id, tool_call_id=tool_call_id)

    from eggthreads import runner as runner_module

    real_stash = runner_module.stash_tool_output_and_build_preview

    def fail_stash(*_args, **_kwargs):
        raise OSError("phase10 artifact unavailable")

    monkeypatch.setattr(
        "eggthreads.runner.stash_tool_output_and_build_preview",
        fail_stash,
    )
    runner = ThreadRunner(
        db,
        thread_id,
        llm=object(),
        tools=_registry(output),
        owner="egg",
    )

    assert asyncio.run(runner.run_once()) is True
    failed = ts.build_tool_call_states(db, thread_id)[tool_call_id]
    assert failed.state == "TC4"
    assert failed.finished_output == output
    assert len(_payloads(db, thread_id, "tool_call.finished")) == 1
    assert _payloads(db, thread_id, "tool_call.output_approval") == []
    output_root = tmp_path / ".egg" / "egg_outputs" / thread_id
    assert not output_root.exists() or list(output_root.iterdir()) == []

    monkeypatch.setattr(
        "eggthreads.runner.stash_tool_output_and_build_preview",
        real_stash,
    )
    assert asyncio.run(
        ThreadRunner(
            db,
            thread_id,
            llm=object(),
            owner="eggw",
            manual_output_recovery=True,
        ).run_once()
    ) is True

    recovered = ts.build_tool_call_states(db, thread_id)[tool_call_id]
    assert recovered.state == "TC6"
    assert recovered.finished_output == output
    assert len(_payloads(db, thread_id, "tool_call.finished")) == 1
    approvals = _payloads(db, thread_id, "tool_call.output_approval")
    assert len(approvals) == 1
    assert approvals[0]["decision"] == "partial"
    assert approvals[0]["decision_source"] == "automatic_policy"
    assert Path(approvals[0]["artifact_path"]).is_dir()
    assert len(list(output_root.iterdir())) == 1


def test_phase10_capped_finish_metadata_survives_recovery(
    tmp_path, monkeypatch
) -> None:
    """Recovery must not describe a capped durable prefix as complete output."""

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("EGG_OUTPUT_OPTIMIZER", raising=False)
    monkeypatch.setattr("eggthreads.runner.MAX_STORED_TOOL_OUTPUT_CHARS", 50)
    monkeypatch.setattr("eggthreads.runner.LONG_OUTPUT_CHAR_THRESHOLD", 10)
    db = ts.ThreadsDB(tmp_path / "capped-recovery.sqlite")
    db.init_schema()
    thread_id = ts.create_root_thread(db, name="phase10 capped recovery")
    tool_call_id = "phase10-capped-recovery-call"
    output = "c" * 80
    _enqueue(db, thread_id, tool_call_id=tool_call_id)

    from eggthreads import output_policy

    real_decide = output_policy.decide_output_publication

    def fail_policy(*_args, **_kwargs):
        raise RuntimeError("phase10 strand capped finish")

    monkeypatch.setattr(
        "eggthreads.output_policy.decide_output_publication",
        fail_policy,
    )
    assert asyncio.run(
        ThreadRunner(
            db,
            thread_id,
            llm=object(),
            tools=_registry(output),
        ).run_once()
    ) is True

    finish = _payloads(db, thread_id, "tool_call.finished")
    assert finish == [
        {
            "tool_call_id": tool_call_id,
            "reason": "success",
            "output": "c" * 50,
            "original_char_count": 80,
            "output_capped": True,
            "publication_presentation": {},
        }
    ]
    stranded = ts.build_tool_call_states(db, thread_id)[tool_call_id]
    assert stranded.finished_original_char_count == 80
    assert stranded.finished_output_capped is True

    monkeypatch.setattr(
        "eggthreads.output_policy.decide_output_publication",
        real_decide,
    )
    assert asyncio.run(
        ThreadRunner(
            db,
            thread_id,
            llm=object(),
            owner="restart",
            manual_output_recovery=True,
        ).run_once()
    ) is True

    approval = _payloads(db, thread_id, "tool_call.output_approval")
    assert len(approval) == 1
    assert approval[0]["original_char_count"] == 80
    assert approval[0]["output_capped"] is True
    assert "Stored output capped at 50 of 80 chars" in approval[0]["preview"]
    artifact_metadata = json.loads(
        (Path(approval[0]["artifact_path"]) / "metadata.json").read_text()
    )
    assert artifact_metadata["stored_char_count"] == 50
    assert artifact_metadata["original_char_count"] == 80
    assert artifact_metadata["capped"] is True


def test_phase10_failed_partial_artifact_write_is_cleaned(tmp_path, monkeypatch) -> None:
    """An interrupted artifact staging write must not leave a random directory."""

    monkeypatch.chdir(tmp_path)
    db = ts.ThreadsDB(tmp_path / "artifact-cleanup.sqlite")
    db.init_schema()
    thread_id = ts.create_root_thread(db, name="phase10 artifact cleanup")
    tool_call_id = "phase10-artifact-cleanup-call"
    _enqueue(db, thread_id, tool_call_id=tool_call_id)

    real_write_text = Path.write_text

    def fail_metadata(self, *args, **kwargs):
        if self.name == "metadata.json":
            raise OSError("phase10 metadata write unavailable")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_metadata)
    assert asyncio.run(
        ThreadRunner(
            db,
            thread_id,
            llm=object(),
            tools=_registry("w" * 120_000),
        ).run_once()
    ) is True

    state = ts.build_tool_call_states(db, thread_id)[tool_call_id]
    assert state.state == "TC4"
    assert _payloads(db, thread_id, "tool_call.output_approval") == []
    output_root = tmp_path / ".egg" / "egg_outputs" / thread_id
    assert not output_root.exists() or list(output_root.iterdir()) == []


def test_phase10_scheduler_bounds_unchanged_persistent_policy_failure(
    tmp_path, monkeypatch
) -> None:
    """One resident scheduler must stop opening streams for unchanged TC4."""

    from eggthreads import runner as runner_module

    db = ts.ThreadsDB(tmp_path / "persistent-policy.sqlite")
    db.init_schema()
    thread_id = ts.create_root_thread(db, name="phase10 persistent policy")
    tool_call_id = "phase10-persistent-policy-call"
    _enqueue(db, thread_id, tool_call_id=tool_call_id)
    db.append_event(
        "phase10-persistent-policy-approval",
        thread_id,
        "tool_call.approval",
        {"tool_call_id": tool_call_id, "decision": "granted"},
    )
    db.append_event(
        "phase10-persistent-policy-start",
        thread_id,
        "tool_call.execution_started",
        {"tool_call_id": tool_call_id},
        invoke_id="phase10-old-owner",
    )
    db.append_event(
        "phase10-persistent-policy-finish",
        thread_id,
        "tool_call.finished",
        {
            "tool_call_id": tool_call_id,
            "reason": "success",
            "output": "durable output",
        },
        invoke_id="phase10-old-owner",
    )

    attempts = 0
    runner_calls = 0
    real_run_once = runner_module.ThreadRunner.run_once

    def fail_policy(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("phase10 persistent policy failure")

    monkeypatch.setattr(
        "eggthreads.output_policy.decide_output_publication",
        fail_policy,
    )

    async def counted_run_once(self):  # type: ignore[no-untyped-def]
        nonlocal runner_calls
        runner_calls += 1
        return await real_run_once(self)

    monkeypatch.setattr(
        runner_module.ThreadRunner,
        "run_once",
        counted_run_once,
    )
    retries = runner_module._ToolOutputRecoveryRetries(
        base_delay_sec=0.0,
        max_delay_sec=0.0,
    )

    async def exercise():  # type: ignore[no-untyped-def]
        scheduler = runner_module.SubtreeScheduler(db, thread_id, llm=object())
        scheduler._output_recovery_retries = retries
        task = asyncio.create_task(scheduler.run_forever(poll_sec=0.001))
        try:
            await asyncio.sleep(0.08)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        return scheduler

    before = len(_event_rows(db, thread_id))
    scheduler = asyncio.run(exercise())
    after = len(_event_rows(db, thread_id))

    assert attempts == runner_module.TOOL_OUTPUT_RECOVERY_MAX_ATTEMPTS
    assert runner_calls == attempts
    recovery_key = runner_module._tool_output_recovery_key_for_call(
        db, thread_id, tool_call_id
    )
    assert recovery_key is not None
    awaitable_backoff = scheduler._output_recovery_retries.disposition(recovery_key)
    assert awaitable_backoff.exhausted is True
    assert after - before == attempts * 2
    assert len(_payloads(db, thread_id, "stream.open")) == attempts
    assert len(_payloads(db, thread_id, "stream.close")) == attempts
    assert _payloads(db, thread_id, "tool_call.output_approval") == []
    assert ts.build_tool_call_states(db, thread_id)[tool_call_id].state == "TC4"


def test_phase10_recovery_retry_budget_is_process_local_and_watermark_scoped(
    tmp_path,
) -> None:
    """Processes are independently bounded; new durable state retries immediately."""

    from eggthreads import runner as runner_module

    db = ts.ThreadsDB(tmp_path / "retry-scope.sqlite")
    db.init_schema()
    thread_id = ts.create_root_thread(db, name="phase10 retry scope")
    tool_call_id = "phase10-retry-scope-call"
    _enqueue(db, thread_id, tool_call_id=tool_call_id)
    db.append_event(
        "phase10-retry-scope-approval",
        thread_id,
        "tool_call.approval",
        {"tool_call_id": tool_call_id, "decision": "granted"},
    )
    db.append_event(
        "phase10-retry-scope-start",
        thread_id,
        "tool_call.execution_started",
        {"tool_call_id": tool_call_id},
        invoke_id="phase10-old-owner",
    )
    db.append_event(
        "phase10-retry-scope-finish",
        thread_id,
        "tool_call.finished",
        {
            "tool_call_id": tool_call_id,
            "reason": "success",
            "output": "first output",
        },
        invoke_id="phase10-old-owner",
    )
    ra = ts.discover_runner_actionable(db, thread_id)
    first_key = runner_module._tool_output_recovery_key(db, ra)
    assert first_key is not None

    process_a = runner_module._ToolOutputRecoveryRetries(
        max_attempts=2,
        base_delay_sec=0.0,
        max_delay_sec=0.0,
    )
    process_b = runner_module._ToolOutputRecoveryRetries(
        max_attempts=2,
        base_delay_sec=0.0,
        max_delay_sec=0.0,
    )
    process_a.record_failure(first_key, now=0.0)
    process_a.record_failure(first_key, now=0.0)
    assert process_a.disposition(first_key, now=0.0).exhausted is True
    assert process_b.disposition(first_key, now=0.0).allowed is True

    db.append_event(
        "phase10-retry-scope-refinish",
        thread_id,
        "tool_call.finished",
        {
            "tool_call_id": tool_call_id,
            "reason": "success",
            "output": "second output",
        },
        invoke_id="phase10-new-owner",
    )
    next_ra = ts.discover_runner_actionable(db, thread_id)
    next_key = runner_module._tool_output_recovery_key(db, next_ra)
    assert next_key is not None
    assert next_key != first_key
    assert process_a.disposition(next_key, now=0.0).allowed is True


def _seed_successful_tc4(
    db: ts.ThreadsDB,
    thread_id: str,
    tool_call_id: str,
    *,
    output: str = "durable output",
    suffix: str = "seed",
) -> None:
    _enqueue(db, thread_id, tool_call_id=tool_call_id)
    db.append_event(
        f"phase10-{suffix}-approval",
        thread_id,
        "tool_call.approval",
        {"tool_call_id": tool_call_id, "decision": "granted"},
    )
    db.append_event(
        f"phase10-{suffix}-start",
        thread_id,
        "tool_call.execution_started",
        {"tool_call_id": tool_call_id},
        invoke_id=f"phase10-{suffix}-owner",
    )
    db.append_event(
        f"phase10-{suffix}-finish",
        thread_id,
        "tool_call.finished",
        {"tool_call_id": tool_call_id, "reason": "success", "output": output},
        invoke_id=f"phase10-{suffix}-owner",
    )


def test_phase10_overlapping_schedulers_share_one_process_retry_budget(
    tmp_path, monkeypatch
) -> None:
    """Resident schedulers in one process must share three attempts total."""

    from eggthreads import runner as runner_module

    db = ts.ThreadsDB(tmp_path / "overlapping-schedulers.sqlite")
    db.init_schema()
    thread_id = ts.create_root_thread(db, name="phase10 overlapping schedulers")
    tool_call_id = "phase10-overlapping-schedulers-call"
    _seed_successful_tc4(db, thread_id, tool_call_id, suffix="overlapping")

    attempts = 0

    def fail_policy(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("phase10 overlapping persistent failure")

    monkeypatch.setattr(
        "eggthreads.output_policy.decide_output_publication",
        fail_policy,
    )
    shared = runner_module._ToolOutputRecoveryRetries(
        base_delay_sec=0.0,
        max_delay_sec=0.0,
    )
    monkeypatch.setattr(
        runner_module,
        "_PROCESS_TOOL_OUTPUT_RECOVERY_RETRIES",
        shared,
    )

    async def exercise() -> None:
        schedulers = [
            runner_module.SubtreeScheduler(db, thread_id, llm=object(), owner=owner)
            for owner in ("egg", "eggw")
        ]
        assert schedulers[0]._output_recovery_retries is shared
        assert schedulers[1]._output_recovery_retries is shared
        tasks = [
            asyncio.create_task(scheduler.run_forever(poll_sec=0.001))
            for scheduler in schedulers
        ]
        try:
            await asyncio.sleep(0.12)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(exercise())

    assert attempts == runner_module.TOOL_OUTPUT_RECOVERY_MAX_ATTEMPTS
    assert len(_payloads(db, thread_id, "stream.open")) == attempts
    assert len(_payloads(db, thread_id, "stream.close")) == attempts
    assert _payloads(db, thread_id, "tool_call.output_approval") == []


def test_phase10_scheduler_wakes_at_deadline_then_parks_exhausted(
    tmp_path, monkeypatch
) -> None:
    """Backoff has one timed wake; exhausted work creates no runner tasks."""

    from eggthreads import runner as runner_module

    db = ts.ThreadsDB(tmp_path / "deadline-wake.sqlite")
    db.init_schema()
    thread_id = ts.create_root_thread(db, name="phase10 deadline wake")
    tool_call_id = "phase10-deadline-wake-call"
    _seed_successful_tc4(db, thread_id, tool_call_id, suffix="deadline")

    attempts = 0
    runner_calls = 0
    real_run_once = runner_module.ThreadRunner.run_once

    def fail_policy(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("phase10 deadline persistent failure")

    async def counted_run_once(self):  # type: ignore[no-untyped-def]
        nonlocal runner_calls
        runner_calls += 1
        return await real_run_once(self)

    monkeypatch.setattr(
        "eggthreads.output_policy.decide_output_publication",
        fail_policy,
    )
    monkeypatch.setattr(
        runner_module.ThreadRunner,
        "run_once",
        counted_run_once,
    )
    retries = runner_module._ToolOutputRecoveryRetries(
        max_attempts=2,
        base_delay_sec=0.05,
        max_delay_sec=0.05,
    )

    async def exercise():  # type: ignore[no-untyped-def]
        scheduler = runner_module.SubtreeScheduler(db, thread_id, llm=object())
        scheduler._output_recovery_retries = retries
        task = asyncio.create_task(scheduler.run_forever(poll_sec=0.001))
        try:
            await asyncio.sleep(0.025)
            assert attempts == 1
            assert runner_calls == 1
            await asyncio.sleep(0.07)
            assert attempts == 2
            calls_at_exhaustion = runner_calls
            await asyncio.sleep(0.05)
            assert runner_calls == calls_at_exhaustion
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        return scheduler

    scheduler = asyncio.run(exercise())
    assert runner_calls == 2
    recovery_key = runner_module._tool_output_recovery_key_for_call(
        db, thread_id, tool_call_id
    )
    assert recovery_key is not None
    assert scheduler._output_recovery_retries.disposition(recovery_key).exhausted
    assert len(_payloads(db, thread_id, "stream.open")) == 2
    assert len(_payloads(db, thread_id, "stream.close")) == 2


def test_phase10_exhausted_scheduler_wakes_for_new_finish_watermark(
    tmp_path, monkeypatch
) -> None:
    """A new durable finish version bypasses the exhausted old-key parking."""

    from eggthreads import runner as runner_module

    db = ts.ThreadsDB(tmp_path / "watermark-wake.sqlite")
    db.init_schema()
    thread_id = ts.create_root_thread(db, name="phase10 watermark wake")
    tool_call_id = "phase10-watermark-wake-call"
    _seed_successful_tc4(db, thread_id, tool_call_id, suffix="watermark")

    attempts = 0
    first_exhausted = asyncio.Event()
    second_watermark_attempted = asyncio.Event()

    def fail_policy(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            first_exhausted.set()
        elif attempts == 3:
            second_watermark_attempted.set()
        raise RuntimeError("phase10 watermark persistent failure")

    monkeypatch.setattr(
        "eggthreads.output_policy.decide_output_publication",
        fail_policy,
    )
    retries = runner_module._ToolOutputRecoveryRetries(
        max_attempts=2,
        base_delay_sec=0.0,
        max_delay_sec=0.0,
    )

    async def exercise() -> None:
        scheduler = runner_module.SubtreeScheduler(db, thread_id, llm=object())
        scheduler._output_recovery_retries = retries
        task = asyncio.create_task(scheduler.run_forever(poll_sec=0.001))
        try:
            await asyncio.wait_for(first_exhausted.wait(), timeout=1)
            await asyncio.sleep(0.02)
            assert attempts == 2
            db.append_event(
                "phase10-watermark-refinish",
                thread_id,
                "tool_call.finished",
                {
                    "tool_call_id": tool_call_id,
                    "reason": "success",
                    "output": "new durable output",
                },
                invoke_id="phase10-new-owner",
            )
            await asyncio.wait_for(second_watermark_attempted.wait(), timeout=1)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(exercise())
    assert attempts == 3
    assert len(_payloads(db, thread_id, "stream.open")) == 3
    assert ts.build_tool_call_states(db, thread_id)[tool_call_id].state == "TC4"


def test_phase10_ordinary_standalone_runners_share_budget_and_manual_override(
    tmp_path, monkeypatch
) -> None:
    """Runner reconstruction cannot reset the bound; manual retry is explicit."""

    db = ts.ThreadsDB(tmp_path / "standalone-budget.sqlite")
    db.init_schema()
    thread_id = ts.create_root_thread(db, name="phase10 standalone budget")
    tool_call_id = "phase10-standalone-budget-call"
    _seed_successful_tc4(db, thread_id, tool_call_id, suffix="standalone")

    attempts = 0

    def fail_policy(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("phase10 standalone persistent failure")

    monkeypatch.setattr(
        "eggthreads.output_policy.decide_output_publication",
        fail_policy,
    )

    results = [
        asyncio.run(
            ThreadRunner(db, thread_id, llm=object(), owner=f"ordinary-{index}").run_once()
        )
        for index in range(6)
    ]
    assert results[:3] == [True, False, False]
    assert results[3:] == [False, False, False]
    assert attempts == 1

    # Force the shared retry deadlines due without replacing the shared store;
    # two more ordinary runner constructions consume the remaining budget.
    from eggthreads import runner as runner_module

    recovery_key = runner_module._tool_output_recovery_key_for_call(
        db, thread_id, tool_call_id
    )
    assert recovery_key is not None
    state = runner_module._PROCESS_TOOL_OUTPUT_RECOVERY_RETRIES._states[recovery_key]
    state.retry_at = 0.0
    assert asyncio.run(ThreadRunner(db, thread_id, llm=object()).run_once()) is True
    state.retry_at = 0.0
    assert asyncio.run(ThreadRunner(db, thread_id, llm=object()).run_once()) is True
    assert attempts == runner_module.TOOL_OUTPUT_RECOVERY_MAX_ATTEMPTS
    assert asyncio.run(ThreadRunner(db, thread_id, llm=object()).run_once()) is False

    assert asyncio.run(
        ThreadRunner(
            db,
            thread_id,
            llm=object(),
            manual_output_recovery=True,
        ).run_once()
    ) is True
    assert attempts == runner_module.TOOL_OUTPUT_RECOVERY_MAX_ATTEMPTS + 1


def test_phase10_retry_store_is_bounded_and_prunes_completed_keys(tmp_path) -> None:
    """Process state has a hard cap and successful/deleted work is removable."""

    from eggthreads import runner as runner_module

    retries = runner_module._ToolOutputRecoveryRetries(
        max_attempts=1,
        base_delay_sec=0.0,
        max_delay_sec=0.0,
        max_tracked=2,
    )
    keys = [
        runner_module._ToolOutputRecoveryKey(
            db_path=str(tmp_path / "threads.sqlite"),
            thread_id=f"thread-{index}",
            tool_call_id=f"call-{index}",
            state_event_seq=index,
        )
        for index in range(3)
    ]
    retries.record_failure(keys[0], now=0.0)
    retries.record_failure(keys[1], now=0.0)
    saturated = retries.record_failure(keys[2], now=0.0)
    assert saturated.attempts == 1
    assert len(retries._states) == 2
    assert retries.disposition(keys[2], now=0.0).exhausted is True

    retries.record_success(keys[0])
    assert len(retries._states) == 1
    retries.record_failure(keys[2], now=0.0)
    assert len(retries._states) == 2

    retries.clear_thread(keys[1].db_path, keys[1].thread_id)
    assert keys[1] not in retries._states
