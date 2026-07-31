from __future__ import annotations

import copy
import json
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from eggflow import Task, keyed
from eggthreads import (
    create_root_thread,
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

TaskFactory = Callable[..., Task]

PHYSICS_ACTOR_SYSTEM_PROMPT = """You are the Actor in a scientific discovery loop.
Your working directory is a Git repository containing the current theory, trusted
copies of the latest evidence, domain instruments, and a committed plan.

Work like a physicist:
1. Read the domain instructions and current evidence.
2. Improve the current world model. It may contain several competing hypotheses.
3. Use the supplied local backtest and planning instruments as often as useful.
4. Select a non-empty plan returned by the planning instrument.
5. Commit that plan with the supplied commit helper.
6. Make no non-ignored edits after the commit, then answer briefly.

An intent freezes one real action together with each relevant model's prediction.
A plan with diverging model predictions is an experiment; it may contain a shared
multi-action setup prefix before the first prediction branch. You propose plans,
but only the trusted Critic may execute real actions.

The Critic evaluates only committed Git HEAD, never your live working tree. It
independently reruns the original backtest and planner against canonical evidence.
A dirty repository, a missing turn commit, an empty plan, or a plan not returned
by the trusted planner is rejected with a fix request. Scratch files should be
.gitignored if they should not be committed.

The Critic keeps a pulled copy of repository history. If you want to abandon the
current theory workspace completely, delete .git and answer; the Critic will
restore its last copy and rehydrate the latest irreversible world state. Reality
always moves forward and cannot be reset by this mechanism.
"""


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
    critic_thread_id: str
    actor_thread_id: str
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


@dataclass(frozen=True)
class PhysicsStrategy:
    """Git-backed scientific discovery implemented as one ActorCritic loop.

    ``prepare`` creates the domain's initial repository files and canonical world
    state. ``critic`` independently validates committed HEAD and may execute real
    actions until a prediction mismatch or experiment branch resolves the plan.
    """

    actor: Agent = field(repr=False, compare=False)
    prepare: TaskFactory = field(repr=False, compare=False)
    critic: Task = field(repr=False, compare=False)
    identity: Any

    def __post_init__(self) -> None:
        if not callable(self.prepare):
            raise TypeError("prepare must construct an Eggflow Task")
        if not isinstance(self.critic, Task):
            raise TypeError("critic must be an Eggflow Task")
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
                    _PhysicsRun(
                        self,
                        runtime.runtime_key,
                        str(runtime.root),
                        physics_id,
                        max_actions,
                        max_cycles,
                    )
                ),
                operation="PhysicsStrategy",
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
                "eggopt.physics.study.v2", self.strategy.identity
            ),
            "_context_limit": None,
        }
        with _operation_scope(context):
            prepare = self.strategy.prepare(
                workspace=workspace,
                outer_context=outer,
                max_actions=self.max_actions,
            )
            if not isinstance(prepare, Task):
                raise TypeError("prepare must construct an Eggflow Task")
            yield _InitializeRepository(prepare, workspace, outer)
            result = yield ActorCritic(
                actor=self.strategy.actor,
                critic=_GitCritic(
                    self.strategy.critic,
                    outer,
                    self.max_actions,
                ),
                actor_prompt=self._prompt,
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

    @staticmethod
    def _prompt(round_number: int, state: Mapping[str, Any]) -> str:
        if round_number == 1:
            return (
                "Inspect the committed repository and domain instructions. Improve the "
                "current theory, use the supplied instruments, commit a non-empty plan, "
                "and leave the repository clean before answering briefly."
            )
        return state["feedback"]


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

    prepare: Task = field(repr=False, compare=False)
    workspace: str
    outer_context: str

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
        prepared = copy.copy(self.prepare)
        _bind_fields(
            prepared,
            {
                "workspace": str(actor),
                "outer_context": self.outer_context,
            },
        )
        yield prepared
        _commit(actor, "[physics] initialize canonical world state")
        _clone_repository(actor, critic)
        return _git_head(actor)


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
            "eggopt.physics.git-critic.v1",
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
        if not _valid_repository(critic_repo):
            if not _valid_repository(actor):
                return Critique.revise(
                    "Neither the Actor repository nor the Critic history copy is valid. "
                    "Recreate the Git repository and commit the current theory and plan."
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
                "last pulled history and latest canonical world state. Start again from "
                "the restored files and create a new clean commit."
            )

        dirty = _git_status(actor)
        meaningful_dirty = "\n".join(
            line
            for line in dirty.splitlines()
            if line[3:] != ".trusted" and not line[3:].startswith(".trusted/")
        )
        if meaningful_dirty:
            return Critique.revise(
                "The Actor repository is dirty. Commit every intended non-ignored file "
                "or add scratch files to .gitignore, then answer again.\n\n"
                + meaningful_dirty
            )

        actor_head = _git_head(actor)
        critic_head = _git_head(critic_repo)
        if actor_head == critic_head:
            return Critique.revise(
                "No new Actor Git commit was created for this turn. Commit the current "
                "theory and a non-empty committed plan, then answer again."
            )

        try:
            _pull(critic_repo, actor)
        except RuntimeError as exc:
            _restore_repository(actor, critic_repo)
            return Critique.revise(
                "The Actor history could not be pulled by the Critic and was restored "
                f"from the last trusted copy. Create a new commit on that history. {exc}"
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

        trusted = critic_repo / ".trusted"
        dirty = _git_status(critic_repo)
        if trusted.exists():
            _git(critic_repo, "add", "-f", ".trusted")
            completed = subprocess.run(
                ["git", "-C", str(critic_repo), "diff", "--cached", "--quiet"],
                check=False,
            )
            if completed.returncode:
                _git(
                    critic_repo,
                    "commit",
                    "-m",
                    f"[physics] trusted Critic result after {actor_head[:12]}",
                )
        elif dirty:
            _commit(
                critic_repo,
                f"[physics] trusted Critic result after {actor_head[:12]}",
            )
        if _git_head(actor) != _git_head(critic_repo):
            if _git_status(actor):
                return Critique.revise(
                    "The Actor repository changed during Critic evaluation. Restore or "
                    "commit a clean state before the trusted result can be synchronized."
                )
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


def _critic_repository(outer_context: Path) -> Path:
    return outer_context / "critic-repository"


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
    if not repository.is_dir():
        return False
    completed = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "--verify", "HEAD"],
        text=True,
        capture_output=True,
        check=False,
    )
    return completed.returncode == 0


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
    "physics_actor_system_prompt",
    "run_physics",
]
