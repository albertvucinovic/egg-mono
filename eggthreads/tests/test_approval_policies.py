from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import eggthreads as ts
from eggthreads.approval import (
    APPROVAL_ABSTAIN,
    APPROVAL_ALLOW,
    APPROVAL_DENY,
    APPROVAL_REQUIRE_HUMAN,
    ApprovalPolicyRegistry,
    ApprovalRequest,
    ApprovalVerdict,
    aggregate_approval_verdicts,
    create_approval_policy_registry,
)
from eggthreads.tools import ToolRegistry


def test_approval_policy_registry_and_aggregation() -> None:
    @dataclass(frozen=True)
    class Policy:
        name: str
        decision: str

        def evaluate(self, request: ApprovalRequest) -> ApprovalVerdict:
            return ApprovalVerdict(self.decision, policy=self.name)

    registry = ApprovalPolicyRegistry()
    registry.register(Policy("allow", APPROVAL_ALLOW))
    registry.register(Policy("human", APPROVAL_REQUIRE_HUMAN))
    registry.register(Policy("deny", APPROVAL_DENY))

    assert registry.names() == ["allow", "human", "deny"]
    assert aggregate_approval_verdicts([ApprovalVerdict(APPROVAL_ALLOW), ApprovalVerdict(APPROVAL_ABSTAIN)]).decision == APPROVAL_ALLOW
    assert aggregate_approval_verdicts([ApprovalVerdict(APPROVAL_ALLOW), ApprovalVerdict(APPROVAL_REQUIRE_HUMAN)]).decision == APPROVAL_REQUIRE_HUMAN
    assert aggregate_approval_verdicts([ApprovalVerdict(APPROVAL_REQUIRE_HUMAN), ApprovalVerdict(APPROVAL_DENY)]).decision == APPROVAL_DENY


def test_default_approval_policy_registry_has_user_origin_policy() -> None:
    registry = create_approval_policy_registry()

    assert registry.names() == ["user_origin", "compact_thread"]


def test_default_approval_policy_allows_compact_thread() -> None:
    registry = create_approval_policy_registry()

    verdict = registry.get("compact_thread").evaluate(
        ApprovalRequest(
            db=None,
            thread_id="tid",
            tool_call_id="tcid",
            tool_name="compact_thread",
            origin="assistant",
            parent_role="assistant",
        )
    )

    assert verdict.decision == APPROVAL_ALLOW


def test_runner_applies_user_origin_approval_policy_and_audits(tmp_path):
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="root")
    tcid = ts.enqueue_user_tool_call(db, tid, "echo", {}, auto_approve=False, hidden=True)

    tools = ToolRegistry()
    tools.register("echo", "Echo", {"type": "object", "properties": {}}, lambda args: "ok")

    runner = ts.ThreadRunner(db, tid, llm=object(), tools=tools)
    assert asyncio.run(runner.run_once()) is True
    assert asyncio.run(runner.run_once()) is True

    state = ts.build_tool_call_states(db, tid)[tcid]
    assert state.approval_decision == "granted"
    assert state.published is True

    rows = db.conn.execute(
        "SELECT payload_json FROM events WHERE thread_id=? AND type='tool_call.approval_policy'",
        (tid,),
    ).fetchall()
    assert rows
    payload = json.loads(rows[0][0])
    assert payload["tool_call_id"] == tcid
    assert payload["final"]["decision"] == "allow"
