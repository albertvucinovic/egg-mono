from __future__ import annotations

import copy
import dataclasses
import json
import math
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from eggflow import Task
from eggthreads import (
    create_root_thread,
    list_root_threads,
    list_threads,
)

from ..actor_critic import ActorCritic, Agent
from ..actor_critic_git import (
    GitCritic,
    _authoritative_state,
    _bind_fields,
    _clone_repository,
    _commit,
    _critic_repository,
    _git,
    _git_head,
    _git_status,
    _initialize_repository,
    _overlay_authoritative_state,
    _pull,
    _restore_repository,
    _valid_repository,
)
from ..context import _operation_scope
from ..identity import digest_payload
from ..runtime import Runtime, sync
from .critic import PhysicsCritic, write_state
from .instruments import (
    ACTOR_INSTRUCTIONS,
    validate_domain_files,
    write_actor_files,
)
from .latent_critic import LatentPhysicsCritic
from .lifecycle import TerminalOutcome, classify_terminal_state, terminal_feedback
from .modes import VERIFIED, PhysicsMode, physics_mode
from .systemprompt import physics_actor_system_prompt

TaskFactory = Callable[..., Task]

PHYSICS_ACTOR_SYSTEM_PROMPT = ACTOR_INSTRUCTIONS


def _actor_turn_prompt(
    round_number: int, state: Mapping[str, Any], *, mode: PhysicsMode = VERIFIED
) -> str:
    if round_number == 1:
        if not mode.planner:
            return (
                "Begin one Physics Actor turn now. Follow the complete runbook in your "
                "system instructions and INSTRUCTIONS.md: inspect Git and canonical "
                "evidence, revise world_model.py, plan in latent space with code you "
                "create as useful, validate plan.json, commit world_model.py and "
                "plan.json with ordinary Git commands, verify a new clean HEAD, then "
                "answer briefly. Do not merely describe the procedure and do not execute "
                "the real environment yourself."
            )
        return (
            "Begin one Physics Actor turn now. Follow the complete runbook in your "
            "system instructions and INSTRUCTIONS.md: inspect Git and canonical "
            "evidence, revise and backtest world_model.py, define matching "
            "goal_<suffix> and useful heuristic_<suffix> capabilities for the default "
            "A* search (plus reward_<suffix> when useful), read the detailed guide in "
            "plan.py, use or adapt that planner (or your own script) to find a productive "
            "trajectory, create and validate plan.json, commit "
            "world_model.py and plan.json with ordinary Git commands, verify a new clean "
            "HEAD, then answer briefly. Do not run any legacy commit.py. Do not merely "
            "describe the procedure and do not execute "
            "the real environment yourself."
        )
    return (
        "The trusted Critic completed the previous proposal and requested another "
        "Physics Actor turn. Read the synchronized canonical-input.json and "
        "trusted-report.json before editing. Follow the complete runbook again, "
        "address the Critic evidence below, and finish with one new clean commit "
        "containing both world_model.py and plan.json. Use ordinary Git commands and do "
        "not run any legacy commit.py.\n\nTrusted Critic feedback:\n"
        + state["feedback"]
    )


@dataclass(frozen=True)
class PhysicsResult:
    """Result of one Git-backed Physics ActorCritic run."""

    value: Any
    accepted: bool
    feedback: str
    stopping_reason: str
    rounds: int
    head: str | None
    physics_thread_id: str
    critic_thread_id: str | None
    actor_thread_id: str | None
    workspace: str

    @property
    def timeline(self) -> tuple[Any, ...]:
        value = self.value
        if isinstance(value, Mapping):
            return tuple(value.get("timeline", ()))
        return tuple(getattr(value, "timeline", ()))

    @property
    def actions(self) -> int:
        value = self.value
        if isinstance(value, Mapping):
            return int(value.get("actions", 0))
        return int(getattr(value, "actions", 0))

    @property
    def goal_reached(self) -> bool:
        """Whether the trusted domain goal, rather than another stop, was reached."""

        return self.stopping_reason == "won"


@dataclass(frozen=True)
class PhysicsStrategy:
    """Git-backed scientific discovery implemented as one ActorCritic loop.

    ``prepare`` creates the domain's initial repository files and canonical world
    state. ``critic`` independently validates committed HEAD and may execute real
    actions until a prediction mismatch or another stopping condition.

    ``default_search_depth`` and ``default_max_nodes`` seed the Actor's editable
    planner defaults; they are not trusted ceilings. Planner suggestions never
    gate submitted trajectories.
    ``domain_files`` lets a domain seed additional root-level text helpers into
    the Actor repository without coupling generic PhysicsStrategy to that domain.

    ``latent``, ``verified``, and ``planner`` are the three behavior flags. The
    established defaults select the ``verified`` strategy: complete public-state
    models, exact verification, and the bundled planner. ``mode`` is the derived
    named view used for durable identity and strategy-specific instruments.
    """

    actor: Agent = field(repr=False, compare=False)
    observe: TaskFactory = field(repr=False, compare=False)
    execute: TaskFactory = field(repr=False, compare=False)
    validate_action: Callable[..., Any] = field(repr=False, compare=False)
    is_goal: Callable[[Any], bool] = field(repr=False, compare=False)
    identity: Any
    latent: bool = False
    verified: bool = True
    planner: bool = True
    terminal_outcome: Callable[[Any], TerminalOutcome | None] | None = field(
        default=None, repr=False, compare=False
    )
    domain_information: str = ""
    domain_files: tuple[tuple[str, str], ...] = ()
    planner_actions: tuple[Any, ...] = ()
    default_search_depth: int = 8
    default_max_nodes: int = 10_000
    evaluator_timeout_sec: float = 300.0

    @classmethod
    def configured(
        cls,
        *,
        latent: bool,
        verified: bool,
        planner: bool,
        **kwargs: Any,
    ) -> PhysicsStrategy:
        """Construct a strategy from the three explicit behavior flags."""

        return cls(
            latent=latent,
            verified=verified,
            planner=planner,
            **kwargs,
        )

    @property
    def mode(self) -> PhysicsMode:
        return physics_mode(
            latent=self.latent,
            verified=self.verified,
            planner=self.planner,
        )

    def __post_init__(self) -> None:
        for name in ("latent", "verified", "planner"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be boolean")
        expected_prompt = physics_actor_system_prompt(
            self.domain_information, mode=self.mode
        )
        if self.actor.system_prompt != expected_prompt:
            object.__setattr__(
                self,
                "actor",
                dataclasses.replace(self.actor, system_prompt=expected_prompt),
            )
        for name in ("observe", "execute", "validate_action", "is_goal"):
            if not callable(getattr(self, name)):
                raise TypeError(f"{name} must be callable")
        if self.terminal_outcome is not None and not callable(self.terminal_outcome):
            raise TypeError("terminal_outcome must be callable or None")
        for name in ("default_search_depth", "default_max_nodes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if not isinstance(self.planner_actions, tuple):
            raise TypeError("planner_actions must be a finite tuple")
        json.dumps(self.planner_actions, allow_nan=False)
        validate_domain_files(self.domain_files)
        if (
            isinstance(self.evaluator_timeout_sec, bool)
            or not isinstance(self.evaluator_timeout_sec, (int, float))
            or not math.isfinite(self.evaluator_timeout_sec)
            or self.evaluator_timeout_sec <= 0
        ):
            raise ValueError("evaluator_timeout_sec must be positive")
        digest_payload("eggopt.physics.identity.v2", self.identity)

    def run(
        self,
        *,
        run_dir: str | Path = ".eggopt/physics",
        max_actions: int = 100,
        max_cycles: int = 100,
    ) -> PhysicsResult:
        """Run or resume one scientific ActorCritic study."""

        for name, value in (("max_actions", max_actions), ("max_cycles", max_cycles)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        with Runtime.open(run_dir) as runtime:
            physics_id = sync(
                runtime.flow.run(_EnsurePhysicsThread(runtime.threads)),
                operation="PhysicsStrategy",
            )
            return sync(
                runtime.flow.run(
                    self.task(
                        runtime_key=runtime.runtime_key,
                        run_dir=runtime.root,
                        physics_thread_id=physics_id,
                        max_actions=max_actions,
                        max_cycles=max_cycles,
                    )
                ),
                operation="PhysicsStrategy",
            )

    def task(
        self,
        *,
        runtime_key: str,
        run_dir: str | Path,
        physics_thread_id: str,
        max_actions: int = 100,
        max_cycles: int = 100,
    ) -> Task:
        """Build this study inside an already-open Eggopt runtime and thread tree.

        This is the composition boundary for a batch/root runner. The caller owns
        the shared :class:`Runtime`, creates the Physics child, and may run one
        :class:`~eggthreads.SubtreeScheduler` across all sibling studies. Set the
        Actor's ``scheduler_managed`` flag when that scheduler, rather than the
        ActorCritic task itself, should drive model turns.
        """

        for name, value in (("max_actions", max_actions), ("max_cycles", max_cycles)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if not isinstance(runtime_key, str) or not runtime_key:
            raise ValueError("runtime_key must be a non-empty string")
        if not isinstance(physics_thread_id, str) or not physics_thread_id:
            raise ValueError("physics_thread_id must be a non-empty string")
        return _PhysicsRun(
            self,
            runtime_key,
            str(Path(run_dir).resolve()),
            physics_thread_id,
            max_actions,
            max_cycles,
        )


def run_physics(
    strategy: PhysicsStrategy,
    *,
    run_dir: str | Path = ".eggopt/physics",
    max_actions: int = 100,
    max_cycles: int = 100,
) -> PhysicsResult:
    """Run or resume ``strategy`` with a compact functional API."""

    if not isinstance(strategy, PhysicsStrategy):
        raise TypeError("strategy must be a PhysicsStrategy")
    return strategy.run(
        run_dir=run_dir,
        max_actions=max_actions,
        max_cycles=max_cycles,
    )


@dataclass
class _EnsurePhysicsThread(Task):
    threads: Any = field(repr=False, compare=False)

    def get_cache_key(self) -> str:
        return digest_payload("eggopt.physics.create-study.v2", {})

    def run(self) -> str:
        roots = [
            thread_id
            for thread_id, name, *_ in _root_threads(self.threads)
            if name == "Physics"
        ]
        if len(roots) > 1:
            raise RuntimeError("Physics run has multiple root threads")
        return roots[0] if roots else create_root_thread(self.threads, name="Physics")


def _root_threads(db: Any) -> list[tuple[str, str, str, str]]:
    root_ids = set(list_root_threads(db))
    return [
        (thread.thread_id, thread.name, thread.short_recap, thread.created_at)
        for thread in list_threads(db)
        if thread.thread_id in root_ids
    ]


@dataclass
class _PhysicsRun(Task):
    cacheable = False

    strategy: PhysicsStrategy = field(repr=False, compare=False)
    runtime_key: str
    run_dir: str
    physics_id: str
    max_actions: int
    max_cycles: int

    def run(self):
        outer = str(Path(self.run_dir) / "workspace")
        workspace = str(Path(outer) / "innerContext")
        context = {
            "operation_thread_id": self.physics_id,
            "evaluation_thread_id": self.physics_id,
            "physics_thread_id": self.physics_id,
            "outer_context": outer,
            "inner_context": workspace,
            "_runtime_key": self.runtime_key,
            "_evaluation_key": digest_payload(
                "eggopt.physics.study.v4.actor-owned-search",
                {
                    "identity": self.strategy.identity,
                    "mode": self.strategy.mode.name,
                    "domain_files": self.strategy.domain_files,
                    "evaluator_timeout_sec": self.strategy.evaluator_timeout_sec,
                },
            ),
            "_context_limit": None,
        }
        with _operation_scope(context):
            observe = self.strategy.observe(workspace=workspace)
            if not isinstance(observe, Task):
                raise TypeError("observe must construct an Eggflow Task")
            yield _InitializeRepository(
                observe,
                workspace,
                outer,
                self.strategy.domain_information,
                self.strategy.mode,
                self.strategy.domain_files,
                self.strategy.planner_actions,
                self.strategy.default_search_depth,
                self.strategy.default_max_nodes,
                self.strategy.evaluator_timeout_sec,
            )
            terminal = _current_terminal_state(outer, self.strategy)
            if terminal is not None:
                value = _terminal_value(outer, terminal)
                return PhysicsResult(
                    value=value,
                    accepted=True,
                    feedback=terminal_feedback(terminal),
                    stopping_reason=terminal,
                    rounds=0,
                    head=_git_head(Path(workspace)),
                    physics_thread_id=self.physics_id,
                    critic_thread_id=None,
                    actor_thread_id=None,
                    workspace=workspace,
                )
            result = yield ActorCritic(
                actor=self.strategy.actor,
                critic=GitCritic(
                    (
                        LatentPhysicsCritic
                        if self.strategy.mode.latent
                        else PhysicsCritic
                    )(
                        tools=self.strategy.actor.tools,
                        execute=self.strategy.execute,
                        validate_action=self.strategy.validate_action,
                        is_goal=self.strategy.is_goal,
                        identity=self.strategy.identity,
                        terminal_outcome=self.strategy.terminal_outcome,
                        evaluator_timeout_sec=self.strategy.evaluator_timeout_sec,
                        mode=self.strategy.mode,
                    ),
                    outer_context=outer,
                    max_actions=self.max_actions,
                    protocol="Physics",
                    required_files=("world_model.py", "plan.json"),
                    trusted_files=("canonical-input.json", "trusted-report.json"),
                    check_commands=(
                        "backtest.py and plan.py"
                        if self.strategy.mode.planner
                        else "your local model and plan checks"
                    ),
                ),
                actor_prompt=lambda round_number, state: _actor_turn_prompt(
                    round_number, state, mode=self.strategy.mode
                ),
                max_rounds=self.max_cycles,
                names=("Actor", "Critic"),
            )
        head = _git_head(Path(workspace))
        value = result.value
        reason = _stopping_reason(value, result.accepted)
        return PhysicsResult(
            value=value,
            accepted=result.accepted,
            feedback=result.feedback,
            stopping_reason=reason,
            rounds=result.rounds,
            head=head,
            physics_thread_id=self.physics_id,
            critic_thread_id=result.critic_thread_id,
            actor_thread_id=result.actor_thread_id,
            workspace=workspace,
        )


def _stopping_reason(value: Any, accepted: bool) -> str:
    if isinstance(value, Mapping):
        reason = value.get("stopping_reason")
        if isinstance(reason, str) and reason:
            return reason
    reason = getattr(value, "stopping_reason", None)
    if isinstance(reason, str) and reason:
        return reason
    return "accepted" if accepted else "max_cycles"


@dataclass
class _InitializeRepository(Task):
    cacheable = False

    observe: Task = field(repr=False, compare=False)
    workspace: str
    outer_context: str
    domain_information: str
    mode: PhysicsMode = VERIFIED
    domain_files: tuple[tuple[str, str], ...] = ()
    planner_actions: tuple[Any, ...] = ()
    default_search_depth: int = 8
    default_max_nodes: int = 10_000
    evaluator_timeout_sec: float = 300.0

    def run(self):
        actor = Path(self.workspace)
        critic = _critic_repository(Path(self.outer_context))
        authoritative = _authoritative_state(Path(self.outer_context))
        if _valid_repository(critic):
            _require_compatible_mode(critic, self.mode)
            if not _valid_repository(actor):
                _restore_repository(actor, critic)
                _overlay_authoritative_state(actor, authoritative)
                _git(actor, "add", "-A")
                if _git_status(actor):
                    _git(
                        actor,
                        "commit",
                        "-m",
                        "[physics] rehydrate latest canonical world state",
                    )
                    _pull(critic, actor)
            return _git_head(actor)
        if _valid_repository(actor):
            _require_compatible_mode(actor, self.mode)
            _clone_repository(actor, critic)
            return _git_head(actor)

        actor.mkdir(parents=True, exist_ok=True)
        if (actor / ".git").exists():
            shutil.rmtree(actor / ".git")
        _initialize_repository(actor)
        observed = copy.copy(self.observe)
        _bind_fields(
            observed,
            {
                "workspace": str(actor),
                "outer_context": self.outer_context,
            },
        )
        initial = yield observed
        write_actor_files(
            actor,
            (initial,),
            self.domain_information,
            instructions=physics_actor_system_prompt(mode=self.mode),
            planner=self.mode.planner,
            mode=self.mode,
            domain_files=self.domain_files,
            planner_actions=self.planner_actions,
            default_search_depth=self.default_search_depth,
            default_max_nodes=self.default_max_nodes,
        )
        write_state(actor, (initial,), 0, None)
        write_state(Path(self.outer_context), (initial,), 0, None)
        _commit(actor, "[physics] initialize canonical world state")
        _clone_repository(actor, critic)
        return _git_head(actor)


def _require_compatible_mode(repository: Path, mode: PhysicsMode) -> None:
    """Prevent one durable run from silently changing its strategy contract."""

    path = repository / "physics-mode.json"
    if not path.is_file():
        if mode is VERIFIED:
            return
        raise ValueError(
            "existing Physics repository predates strategy modes; start a new run "
            f"directory for {mode.name}"
        )
    value = json.loads(path.read_text())
    expected = {
        "latent": mode.latent,
        "verified": mode.verified,
        "planner": mode.planner,
    }
    if value != expected:
        raise ValueError(
            f"Physics strategy mode changed for existing run: {value} != {expected}; "
            "use a new run directory"
        )


def _current_terminal_state(
    outer_context: str, strategy: PhysicsStrategy
) -> str | None:
    state = json.loads(
        (_authoritative_state(Path(outer_context)) / "state.json").read_text()
    )
    timeline = tuple(state["timeline"])
    current = timeline[-1].get("next_state", timeline[-1])
    return classify_terminal_state(
        current,
        is_goal=strategy.is_goal,
        terminal_outcome=strategy.terminal_outcome,
    )


def _terminal_value(outer_context: str, reason: str) -> dict[str, Any]:
    state = json.loads(
        (_authoritative_state(Path(outer_context)) / "state.json").read_text()
    )
    report = {
        "stage": "execution",
        "resolution": reason,
        "actions": int(state["actions"]),
        "executed": [],
    }
    return {
        "stopping_reason": reason,
        "timeline": tuple(state["timeline"]),
        "actions": int(state["actions"]),
        "report": report,
    }


__all__ = [
    "PHYSICS_ACTOR_SYSTEM_PROMPT",
    "PhysicsResult",
    "PhysicsStrategy",
    "TerminalOutcome",
    "physics_actor_system_prompt",
    "run_physics",
]
