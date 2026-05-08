from __future__ import annotations

"""Approval policy plugin seam.

Policies return verdicts only. Core remains responsible for recording
``tool_call.approval`` events and advancing the TC state machine.
"""

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Protocol, runtime_checkable


APPROVAL_ALLOW = "allow"
APPROVAL_DENY = "deny"
APPROVAL_REQUIRE_HUMAN = "require_human"
APPROVAL_ABSTAIN = "abstain"


@dataclass(frozen=True)
class ApprovalRequest:
    db: Any
    thread_id: str
    tool_call_id: str
    tool_name: str
    arguments: Any = None
    origin: str = "assistant"
    parent_role: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ApprovalVerdict:
    decision: str
    reason: str = ""
    policy: str = ""

    def __post_init__(self) -> None:
        if self.decision not in {
            APPROVAL_ALLOW,
            APPROVAL_DENY,
            APPROVAL_REQUIRE_HUMAN,
            APPROVAL_ABSTAIN,
        }:
            raise ValueError(f"Unknown approval verdict: {self.decision}")


@runtime_checkable
class ApprovalPolicy(Protocol):
    name: str

    def evaluate(self, request: ApprovalRequest) -> ApprovalVerdict:
        ...


class ApprovalPolicyRegistry:
    """Deterministic registry for approval verdict providers."""

    def __init__(self) -> None:
        self._policies: Dict[str, ApprovalPolicy] = {}

    def register(self, policy: ApprovalPolicy) -> None:
        name = str(getattr(policy, "name", "") or "").strip()
        if not name:
            raise ValueError("Approval policy name must not be empty")
        if name in self._policies:
            raise ValueError(f"Approval policy already registered: {name}")
        self._policies[name] = policy

    def get(self, name: str) -> Optional[ApprovalPolicy]:
        return self._policies.get(name)

    def names(self) -> List[str]:
        return list(self._policies.keys())

    def policies(self) -> List[ApprovalPolicy]:
        return list(self._policies.values())


def aggregate_approval_verdicts(verdicts: Iterable[ApprovalVerdict]) -> ApprovalVerdict:
    """Conservative composition: deny > require_human > allow > abstain."""

    saw_allow: Optional[ApprovalVerdict] = None
    saw_human: Optional[ApprovalVerdict] = None
    for verdict in verdicts:
        if verdict.decision == APPROVAL_DENY:
            return verdict
        if verdict.decision == APPROVAL_REQUIRE_HUMAN and saw_human is None:
            saw_human = verdict
        elif verdict.decision == APPROVAL_ALLOW and saw_allow is None:
            saw_allow = verdict
    if saw_human is not None:
        return saw_human
    if saw_allow is not None:
        return saw_allow
    return ApprovalVerdict(APPROVAL_ABSTAIN, reason="No approval policy decided")


def _emit_audit_event(db: Any, thread_id: str, request: ApprovalRequest, verdicts: List[ApprovalVerdict], final: ApprovalVerdict) -> None:
    try:
        db.append_event(
            event_id=os.urandom(10).hex(),
            thread_id=thread_id,
            type_="tool_call.approval_policy",
            msg_id=None,
            invoke_id=None,
            payload={
                "tool_call_id": request.tool_call_id,
                "tool_name": request.tool_name,
                "origin": request.origin,
                "verdicts": [
                    {"policy": v.policy, "decision": v.decision, "reason": v.reason}
                    for v in verdicts
                ],
                "final": {"policy": final.policy, "decision": final.decision, "reason": final.reason},
            },
        )
    except Exception:
        pass


def evaluate_approval_policies(registry: ApprovalPolicyRegistry, request: ApprovalRequest, *, audit: bool = True) -> ApprovalVerdict:
    verdicts: List[ApprovalVerdict] = []
    for policy in registry.policies():
        try:
            verdict = policy.evaluate(request)
        except Exception as e:
            verdict = ApprovalVerdict(APPROVAL_REQUIRE_HUMAN, reason=f"Policy error: {e}", policy=getattr(policy, "name", ""))
        if not verdict.policy:
            verdict = ApprovalVerdict(verdict.decision, verdict.reason, getattr(policy, "name", ""))
        verdicts.append(verdict)
    final = aggregate_approval_verdicts(verdicts)
    if audit and request.db is not None:
        _emit_audit_event(request.db, request.thread_id, request, verdicts, final)
    return final


def create_approval_policy_registry() -> ApprovalPolicyRegistry:
    from .builtin_plugins.approval_policies import ApprovalPoliciesPlugin
    from .plugins import ProviderPluginContext, register_plugins

    registry = ApprovalPolicyRegistry()
    register_plugins(ProviderPluginContext(approval_policy_registry=registry), [ApprovalPoliciesPlugin()])
    return registry


def _coerce_arguments(arguments: Any) -> Any:
    if isinstance(arguments, str):
        try:
            return json.loads(arguments) if arguments.strip() else {}
        except Exception:
            return arguments
    return arguments


def request_from_tool_call_state(db: Any, thread_id: str, tc: Any, *, origin: str = "assistant") -> ApprovalRequest:
    return ApprovalRequest(
        db=db,
        thread_id=thread_id,
        tool_call_id=str(getattr(tc, "tool_call_id", "")),
        tool_name=str(getattr(tc, "name", "")),
        arguments=_coerce_arguments(getattr(tc, "arguments", None)),
        origin=origin,
        parent_role=getattr(tc, "parent_role", None),
    )


__all__ = [
    "APPROVAL_ABSTAIN",
    "APPROVAL_ALLOW",
    "APPROVAL_DENY",
    "APPROVAL_REQUIRE_HUMAN",
    "ApprovalPolicy",
    "ApprovalPolicyRegistry",
    "ApprovalRequest",
    "ApprovalVerdict",
    "aggregate_approval_verdicts",
    "create_approval_policy_registry",
    "evaluate_approval_policies",
    "request_from_tool_call_state",
]
