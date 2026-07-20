"""Domain-neutral optimization values and Producer composition."""

from .core import (
    Advance,
    Candidate,
    CaseEvidence,
    Feedback,
    FunctionProducer,
    JSONValue,
    Metric,
    Observation,
    Producer,
    Proposal,
    Stop,
    StrategyDecision,
    StrategyInput,
)
from .evaluation import CaseRequest, EvaluationRequest
from .gepa import GEPAState, GEPAStrategy
from .physics import PhysicsState, PhysicsStrategy
from .runtime import (
    OperationContext,
    OperationInput,
    OperationResult,
    ProposalResult,
    StepResult,
    StrategyRunInput,
    StrategyRunResult,
)
from .repair import (
    Accepted,
    Inspection,
    ItemFailure,
    NeedsRepair,
    RepairFeedback,
)

__all__ = [
    "Advance",
    "Accepted",
    "Candidate",
    "CaseEvidence",
    "CaseRequest",
    "EvaluationRequest",
    "Feedback",
    "FunctionProducer",
    "GEPAState",
    "GEPAStrategy",
    "Inspection",
    "ItemFailure",
    "JSONValue",
    "Metric",
    "NeedsRepair",
    "Observation",
    "OperationContext",
    "OperationInput",
    "OperationResult",
    "PhysicsState",
    "PhysicsStrategy",
    "Producer",
    "Proposal",
    "ProposalResult",
    "RepairFeedback",
    "Stop",
    "StepResult",
    "StrategyDecision",
    "StrategyInput",
    "StrategyRunInput",
    "StrategyRunResult",
]
