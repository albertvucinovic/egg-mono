from __future__ import annotations

"""Output publication policy seam.

The raw tool output remains in ``tool_call.finished``. Policies decide what
preview/content is published to UI/LLM channels while core keeps hard size
limits and event publication authority.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class OutputChannels:
    raw: str = "raw"
    artifact: str = "artifact"
    ui_preview: str = "ui_preview"
    llm_message: str = "llm_message"
    audit: str = "audit"


OUTPUT_CHANNELS = OutputChannels()


@dataclass(frozen=True)
class OutputPolicyRequest:
    db: Any
    thread_id: str
    tool_call_id: str
    tool_name: str = ""
    output: str = ""
    origin: str = "runner"
    tool_metadata: Mapping[str, Any] = field(default_factory=dict)
    thread_config: Mapping[str, Any] = field(default_factory=dict)
    limits: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    tool_args: Mapping[str, Any] = field(default_factory=dict)
    finished_reason: str = ""
    user_tool_call: bool = False


@dataclass(frozen=True)
class OutputPublicationDecision:
    decision: str
    preview: str
    reason: str = ""
    artifact_path: str = ""
    channels: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class OutputPolicy(Protocol):
    name: str

    def decide(self, request: OutputPolicyRequest) -> OutputPublicationDecision:
        ...


class OutputPolicyRegistry:
    def __init__(self) -> None:
        self._policies: Dict[str, OutputPolicy] = {}

    def register(self, policy: OutputPolicy) -> None:
        name = str(getattr(policy, "name", "") or "").strip()
        if not name:
            raise ValueError("Output policy name must not be empty")
        if name in self._policies:
            raise ValueError(f"Output policy already registered: {name}")
        self._policies[name] = policy

    def get(self, name: str) -> Optional[OutputPolicy]:
        return self._policies.get(name)

    def names(self) -> List[str]:
        return list(self._policies.keys())

    def policies(self) -> List[OutputPolicy]:
        return list(self._policies.values())


def create_output_policy_registry() -> OutputPolicyRegistry:
    from .builtin_plugins.output_policies import OutputPoliciesPlugin
    from .plugins import ProviderPluginContext, register_plugins

    registry = OutputPolicyRegistry()
    register_plugins(ProviderPluginContext(output_policy_registry=registry), [OutputPoliciesPlugin()])
    return registry


def decide_output_publication(registry: OutputPolicyRegistry, request: OutputPolicyRequest) -> OutputPublicationDecision:
    """Compose output policy decisions in registry order.

    Policies are advisory. Later policies may refine earlier decisions; a
    decision value of ``"abstain"`` leaves the current decision unchanged. A
    policy that needs to preserve/override metadata from earlier policies may
    implement ``decide_with_current(request, current_decision)``. If no policy
    decides, core falls back to publishing the whole output.
    """

    decision: OutputPublicationDecision | None = None
    for policy in registry.policies():
        decide_with_current = getattr(policy, "decide_with_current", None)
        if callable(decide_with_current):
            proposed = decide_with_current(request, decision)
        else:
            proposed = policy.decide(request)
        if proposed.decision == "abstain":
            continue
        decision = proposed
    if decision is None:
        return OutputPublicationDecision("whole", request.output, reason="No output policy")
    return decision


__all__ = [
    "OUTPUT_CHANNELS",
    "OutputChannels",
    "OutputPolicy",
    "OutputPolicyRegistry",
    "OutputPolicyRequest",
    "OutputPublicationDecision",
    "create_output_policy_registry",
    "decide_output_publication",
]
