from __future__ import annotations

"""Built-in deterministic approval policies."""

from dataclasses import dataclass
from typing import Any

from ..approval import APPROVAL_ABSTAIN, APPROVAL_ALLOW, ApprovalRequest, ApprovalVerdict
from ..plugins import PluginContext


@dataclass(frozen=True)
class UserOriginApprovalPolicy:
    """Allow user-originated RA3 tool calls to preserve existing behavior."""

    name: str = "user_origin"

    def evaluate(self, request: ApprovalRequest) -> ApprovalVerdict:
        if request.origin == "user_command" or request.parent_role == "user":
            return ApprovalVerdict(APPROVAL_ALLOW, reason="User-originated tool call", policy=self.name)
        return ApprovalVerdict(APPROVAL_ABSTAIN, reason="Not a user-originated tool call", policy=self.name)


def register_approval_policies(registry: Any) -> None:
    registry.register(UserOriginApprovalPolicy())


@dataclass(frozen=True)
class ApprovalPoliciesPlugin:
    name: str = "approval_policies"
    version: str = "0"

    def register(self, context: PluginContext) -> None:
        if context.approval_policy_registry is not None:
            register_approval_policies(context.approval_policy_registry)


__all__ = ["ApprovalPoliciesPlugin", "UserOriginApprovalPolicy", "register_approval_policies"]
