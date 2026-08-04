from __future__ import annotations

import copy
import json
import math
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from eggflow import Task, keyed
from eggthreads import (
    create_root_thread,
    get_thread_sandbox_config,
    get_thread_working_directory,
    list_root_threads,
    list_threads,
    set_thread_sandbox_config,
    set_thread_tools_enabled,
    set_thread_working_directory,
)

from ..actor_critic import ActorCritic, Agent, Critique
from ..context import _current_operation, _operation_runtime, _operation_scope
from ..identity import digest_payload
from ..runtime import Runtime, sync
from .critic import PhysicsCritic, write_state
from .instruments import (
    ACTOR_INSTRUCTIONS,
    validate_domain_files,
    write_actor_files,
)
from .lifecycle import TerminalOutcome, classify_terminal_state, terminal_feedback

TaskFactory = Callable[..., Task]

PHYSICS_ACTOR_SYSTEM_PROMPT = ACTOR_INSTRUCTIONS


def _actor_turn_prompt(round_number: int, state: Mapping[str, Any]) -> str:
    if round_number == 1:
        return (
            "Begin one Physics Actor turn now. Follow the complete runbook in your "
            "system instructions and INSTRUCTIONS.md: inspect Git and canonical "
            "evidence, revise and backtest world_model.py, define useful matching "
            "reward_<suffix> and/or goal_<suffix> planning capabilities, read the "
            "detailed guide in plan.py, use or adapt that planner (or your own script) "
            "to find a productive trajectory, create and validate plan.json, commit "
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


def physics_actor_system_prompt(domain_information: str = "") -> str:
    """Return the canonical Physics Actor rules plus domain-specific guidance."""

    domain_information = str(domain_information or "").strip()
    if not domain_information:
        return PHYSICS_ACTOR_SYSTEM_PROMPT.strip()
    return (
        PHYSICS_ACTOR_SYSTEM_PROMPT.strip()
        + "\n\n## Domain information\n\n"
        + domain_information
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
    """

    actor: Agent = field(repr=False, compare=False)
    observe: TaskFactory = field(repr=False, compare=False)
    execute: TaskFactory = field(repr=False, compare=False)
    validate_action: Callable[..., Any] = field(repr=False, compare=False)
    is_goal: Callable[[Any], bool] = field(repr=False, compare=False)
    identity: Any
    terminal_outcome: Callable[[Any], TerminalOutcome | None] | None = field(
        default=None, repr=False, compare=False
    )
    domain_information: str = ""
    domain_files: tuple[tuple[str, str], ...] = ()
    planner_actions: tuple[Any, ...] = ()
    default_search_depth: int = 8
    default_max_nodes: int = 10_000
    evaluator_timeout_sec: float = 300.0

    def __post_init__(self) -> None:
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
                critic=_GitCritic(
                    PhysicsCritic(
                        tools=self.strategy.actor.tools,
                        execute=self.strategy.execute,
                        validate_action=self.strategy.validate_action,
                        is_goal=self.strategy.is_goal,
                        identity=self.strategy.identity,
                        terminal_outcome=self.strategy.terminal_outcome,
                        evaluator_timeout_sec=self.strategy.evaluator_timeout_sec,
                    ),
                    outer,
                    self.max_actions,
                ),
                actor_prompt=_actor_turn_prompt,
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


@dataclass
class _GitCritic(Task):
    critic: Task = field(repr=False, compare=False)
    outer_context: str
    max_actions: int
    workspace: str | None = None
    actor_thread_id: str | None = None
    critic_thread_id: str | None = None
    answer: Any = None
    feedback: str = ""
    round_number: int | None = None

    def get_cache_key(self) -> str:
        actor = Path(self.workspace) if self.workspace else None
        head = _git_head(actor) if actor is not None else None
        return digest_payload(
            "eggopt.physics.git-critic.v2.exact-root-isolation",
            {
                "critic": _task_identity(self.critic),
                "outer_context": self.outer_context,
                "max_actions": self.max_actions,
                "head": head or "invalid-repository",
            },
        )

    def run(self):
        if self.workspace is None or self.critic_thread_id is None:
            raise RuntimeError(
                "Physics Critic was not assigned its workspace and thread"
            )
        actor = Path(self.workspace)
        critic_repo = _critic_repository(Path(self.outer_context))
        context = _current_operation()
        db = _operation_runtime(str(context["_runtime_key"]))
        if self.actor_thread_id is None:
            raise RuntimeError("Physics Critic was not assigned its Actor thread")
        _require_thread_isolation(
            db,
            self.actor_thread_id,
            actor,
            role="Actor",
        )
        if not _valid_repository(critic_repo):
            if not _valid_repository(actor):
                return Critique.revise(
                    "Neither the Actor workspace nor the Critic's trusted history copy "
                    "is a valid Git repository. No real action was attempted. Recreate "
                    "the Actor repository from the canonical files, run backtest.py and "
                    "plan.py as useful, then commit world_model.py and plan.json with "
                    "ordinary Git commands."
                )
            _clone_repository(actor, critic_repo)

        if not _valid_repository(actor):
            _restore_repository(actor, critic_repo)
            _overlay_authoritative_state(
                actor, _authoritative_state(Path(self.outer_context))
            )
            _git(actor, "add", "-A")
            if _git_status(actor):
                _git(
                    actor,
                    "commit",
                    "-m",
                    "[physics] rehydrate latest canonical world state",
                )
                _pull(critic_repo, actor)
            return Critique.revise(
                "The Actor repository was missing or corrupt, so the Critic restored its "
                "last pulled history and overlaid the latest irreversible canonical "
                "state. No real action was attempted for this proposal. Read the restored "
                "canonical-input.json and trusted-report.json, rebuild the proposal, and "
                "commit world_model.py and plan.json with ordinary Git commands."
            )

        dirty = _git_status(actor)
        meaningful_dirty = "\n".join(
            line
            for line in dirty.splitlines()
            if line[3:] != ".trusted" and not line[3:].startswith(".trusted/")
        )
        if meaningful_dirty:
            return Critique.revise(
                "The Critic evaluates only a clean committed HEAD, but the Actor "
                "workspace contains the non-ignored changes listed below. No real action "
                "was attempted. Commit intended theory/plan changes with ordinary Git "
                "commands, including world_model.py and plan.json, or move disposable "
                "work under scratch/ or "
                "ignore it, verify `git status --short` is empty, then answer again.\n\n"
                + meaningful_dirty
            )

        actor_head = _git_head(actor)
        critic_head = _git_head(critic_repo)
        if actor_head == critic_head:
            return Critique.revise(
                "This turn did not create a new Actor Git HEAD, so there is no proposal "
                "for the Critic to validate and no real action was attempted. Revise the "
                "theory as needed, run local checks as useful, commit world_model.py and "
                "plan.json with ordinary Git commands, verify a clean new HEAD, then answer."
            )

        try:
            _pull(critic_repo, actor)
        except RuntimeError as exc:
            _restore_repository(actor, critic_repo)
            return Critique.revise(
                "The submitted Actor history was not a fast-forward continuation of the "
                "Critic's trusted copy. No real action was attempted. The Actor workspace "
                "was restored to trusted history; recreate the proposal as a new commit "
                f"on that history. Git detail: {exc}"
            )

        self._configure_critic_workspace(critic_repo)
        domain = copy.copy(self.critic)
        _bind_fields(
            domain,
            {
                "workspace": str(critic_repo),
                "actor_workspace": str(actor),
                "outer_context": self.outer_context,
                "head": actor_head,
                "actor_thread_id": self.actor_thread_id,
                "critic_thread_id": self.critic_thread_id,
                "answer": self.answer,
                "feedback": self.feedback,
                "round_number": self.round_number,
                "max_actions": self.max_actions,
            },
        )
        result = yield keyed(domain, actor_head)

        _commit_trusted_result(critic_repo, actor_head)
        if _git_head(actor) != _git_head(critic_repo):
            actor_dirty = "\n".join(
                line
                for line in _git_status(actor).splitlines()
                if line[3:] not in {"canonical-input.json", "trusted-report.json"}
                and line[3:] != ".trusted"
                and not line[3:].startswith(".trusted/")
            )
            if actor_dirty:
                return Critique.revise(
                    "The Actor workspace changed after it submitted HEAD while the Critic "
                    "was independently evaluating that commit. The trusted result cannot "
                    "be synchronized over those edits. Preserve intended work separately, "
                    "restore a clean synchronized repository, and submit it in a new "
                    "Actor commit; do not edit files after submitting the commit."
                )
            _git(actor, "reset", "--hard", "HEAD")
            _git(actor, "clean", "-fd")
            _pull(actor, critic_repo)
        return result

    def _configure_critic_workspace(self, repository: Path) -> None:
        context = _current_operation()
        db = _operation_runtime(str(context["_runtime_key"]))
        set_thread_working_directory(
            db,
            self.critic_thread_id,
            str(repository),
            reason="Physics Critic independent repository",
        )
        set_thread_tools_enabled(db, self.critic_thread_id, True)
        set_thread_sandbox_config(
            db,
            self.critic_thread_id,
            enabled=True,
            provider="docker",
            settings={
                "network": {"allowedDomains": [], "deniedDomains": []},
                "workspace": "/workspace",
                "filesystem": {
                    "allowWrite": ["."],
                    "denyWrite": [".egg"],
                    "denyRead": [".egg"],
                },
                "extra_mounts": [],
                "extra_args": ["--cap-drop", "ALL"],
            },
            user_control_enabled=False,
            reason="Physics Critic independent evaluation",
        )
        _require_thread_isolation(
            db,
            self.critic_thread_id,
            repository,
            role="Critic",
        )


def _critic_repository(outer_context: Path) -> Path:
    return outer_context / "critic-repository"


def _require_thread_isolation(
    db, thread_id: str, repository: Path, *, role: str
) -> None:
    """Fail closed unless an Eggthread owns this exact sandboxed repository."""

    working_directory = get_thread_working_directory(db, thread_id).resolve()
    if working_directory != repository.resolve():
        raise RuntimeError(
            f"Physics {role} thread working directory escaped its repository: "
            f"{working_directory} != {repository.resolve()}"
        )
    sandbox = get_thread_sandbox_config(db, thread_id)
    if not sandbox.enabled or not sandbox.provider:
        raise RuntimeError(f"Physics {role} thread has no enabled Eggthreads sandbox")


def _authoritative_state(outer_context: Path) -> Path:
    return outer_context / ".trusted"


def _overlay_authoritative_state(actor: Path, authoritative: Path) -> None:
    if not authoritative.is_dir():
        return
    target = actor / ".trusted"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(authoritative, target)
    state = authoritative / "state.json"
    if state.is_file():
        try:
            timeline = json.loads(state.read_text())["timeline"]
        except (json.JSONDecodeError, KeyError, TypeError):
            return
        (actor / "canonical-input.json").write_text(
            json.dumps({"timeline": timeline}, indent=2, sort_keys=True) + "\n"
        )


def _git(repository: Path, *args: str, check: bool = True) -> str:
    if args and args[0] != "init" and not _is_repository_root(repository):
        raise RuntimeError(
            f"Git target is not an exact repository root: {repository.resolve()}"
        )
    completed = subprocess.run(
        ["git", "-C", str(repository), *args],
        text=True,
        capture_output=True,
        check=False,
    )
    if check and completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(detail or f"git {' '.join(args)} failed")
    return completed.stdout.strip()


def _valid_repository(repository: Path) -> bool:
    """Return whether ``repository`` itself, not an ancestor, is a Git worktree."""

    if not _is_repository_root(repository):
        return False
    head = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "--verify", "HEAD"],
        text=True,
        capture_output=True,
        check=False,
    )
    return head.returncode == 0


def _is_repository_root(repository: Path) -> bool:
    """Return whether Git resolves this directory itself as the worktree root."""

    if not repository.is_dir():
        return False
    completed = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "--show-toplevel"],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode or not completed.stdout.strip():
        return False
    try:
        top = Path(completed.stdout.strip()).resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    return top == repository.resolve()


def _git_head(repository: Path | None) -> str | None:
    if repository is None or not _valid_repository(repository):
        return None
    return _git(repository, "rev-parse", "HEAD")


def _git_status(repository: Path) -> str:
    return _git(repository, "status", "--short", "--untracked-files=normal")


def _configure_git(repository: Path) -> None:
    _git(repository, "config", "user.name", "Egg Physics")
    _git(repository, "config", "user.email", "physics@entropygradient.ai")


def _initialize_repository(repository: Path) -> None:
    _git(repository, "init", "-b", "main")
    _configure_git(repository)


def _commit(repository: Path, message: str) -> None:
    _configure_git(repository)
    _git(repository, "add", "-A")
    if _git_status(repository):
        _git(repository, "commit", "-m", message)


def _commit_trusted_result(repository: Path, actor_head: str) -> None:
    """Commit one complete Critic result, including its public projection."""

    trusted = repository / ".trusted"
    if trusted.exists():
        _git(repository, "add", "-f", ".trusted")
    _commit(
        repository,
        f"[physics] trusted Critic result after {actor_head[:12]}",
    )


def _clone_repository(source: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        ["git", "clone", "--no-local", str(source), str(destination)],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode:
        raise RuntimeError((completed.stderr or completed.stdout).strip())
    _configure_git(destination)
    info_exclude = destination / ".git" / "info" / "exclude"
    with info_exclude.open("a", encoding="utf-8") as stream:
        stream.write("\n.trusted/\n")
    (destination / ".trusted").mkdir(exist_ok=True)


def _restore_repository(actor: Path, critic: Path) -> None:
    if actor.exists():
        shutil.rmtree(actor)
    _clone_repository(critic, actor)


def _pull(destination: Path, source: Path) -> None:
    _git(destination, "reset", "--hard", "HEAD")
    _git(destination, "clean", "-fd")
    _git(destination, "pull", "--ff-only", str(source), "HEAD")


def _bind_fields(task: Task, values: Mapping[str, Any]) -> Task:
    fields = getattr(task, "__dataclass_fields__", {})
    for name, value in values.items():
        if name in fields:
            setattr(task, name, value)
    return task


def _task_identity(task: Task) -> Mapping[str, str]:
    return {
        "module": task.__class__.__module__,
        "name": task.__class__.__qualname__,
        "key": task.get_cache_key(),
    }


__all__ = [
    "PHYSICS_ACTOR_SYSTEM_PROMPT",
    "PhysicsResult",
    "PhysicsStrategy",
    "TerminalOutcome",
    "physics_actor_system_prompt",
    "run_physics",
]
