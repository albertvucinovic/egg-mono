from __future__ import annotations

import copy
import json
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from eggflow import Task, keyed
from eggthreads import (
    get_thread_sandbox_config,
    get_thread_working_directory,
    set_thread_sandbox_config,
    set_thread_tools_enabled,
    set_thread_working_directory,
)

from .actor_critic import Critique
from .context import _current_operation, _operation_runtime
from .identity import digest_payload

TRUSTED_STATE_DIRECTORY = ".trusted"


@dataclass
class GitCritic(Task):
    """Evaluate exactly one new clean Actor commit in an isolated Git clone.

    The wrapped Critic task remains domain-specific. This reusable adapter owns
    repository integrity, fast-forward history, Critic isolation, trusted-result
    commits, and synchronization back to the Actor workspace.
    """

    critic: Task = field(repr=False, compare=False)
    outer_context: str
    max_actions: int
    protocol: str = "actor-critic"
    required_files: tuple[str, ...] = ()
    check_commands: str = "local checks"
    trusted_files: tuple[str, ...] = ()
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
            "eggopt.git-critic.v1.exact-root-isolation",
            {
                "critic": _task_identity(self.critic),
                "outer_context": self.outer_context,
                "max_actions": self.max_actions,
                "protocol": self.protocol,
                "required_files": self.required_files,
                "trusted_files": self.trusted_files,
                "head": head or "invalid-repository",
            },
        )

    def run(self):
        if self.workspace is None or self.critic_thread_id is None:
            raise RuntimeError(
                f"{self.protocol} Critic was not assigned its workspace and thread"
            )
        actor = Path(self.workspace)
        critic_repo = _critic_repository(Path(self.outer_context))
        context = _current_operation()
        db = _operation_runtime(str(context["_runtime_key"]))
        if self.actor_thread_id is None:
            raise RuntimeError(
                f"{self.protocol} Critic was not assigned its Actor thread"
            )
        _require_thread_isolation(
            db,
            self.actor_thread_id,
            actor,
            role="Actor",
            protocol=self.protocol,
        )
        if not _valid_repository(critic_repo):
            if not _valid_repository(actor):
                return Critique.revise(
                    "Neither the Actor workspace nor the Critic's trusted history copy "
                    "is a valid Git repository. No real action was attempted. Recreate "
                    f"the Actor repository from canonical files, run {self.check_commands} "
                    f"as useful, then commit {self._required_files()} with ordinary Git "
                    "commands."
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
                f"commit {self._required_files()} with ordinary Git commands."
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
                f"commands, including {self._required_files()}, or move disposable "
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
                f"proposal as needed, run {self.check_commands} as useful, commit "
                f"{self._required_files()}, verify a clean new HEAD, then answer."
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

        _commit_trusted_result(critic_repo, actor_head, self._commit_prefix())
        if _git_head(actor) != _git_head(critic_repo):
            trusted_files = set(self.trusted_files)
            actor_dirty = "\n".join(
                line
                for line in _git_status(actor).splitlines()
                if line[3:] not in trusted_files
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
            reason=f"{self.protocol} Critic independent repository",
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
            reason=f"{self.protocol} Critic independent evaluation",
        )
        _require_thread_isolation(
            db,
            self.critic_thread_id,
            repository,
            role="Critic",
            protocol=self.protocol,
        )

    def _required_files(self) -> str:
        return " and ".join(self.required_files) or "the intended proposal files"

    def _commit_prefix(self) -> str:
        return self.protocol.lower().replace(" ", "-")



def _critic_repository(outer_context: Path) -> Path:
    return outer_context / "critic-repository"


def _require_thread_isolation(
    db, thread_id: str, repository: Path, *, role: str, protocol: str = "actor-critic"
) -> None:
    """Fail closed unless an Eggthread owns this exact sandboxed repository."""

    working_directory = get_thread_working_directory(db, thread_id).resolve()
    if working_directory != repository.resolve():
        raise RuntimeError(
            f"{protocol} {role} thread working directory escaped its repository: "
            f"{working_directory} != {repository.resolve()}"
        )
    sandbox = get_thread_sandbox_config(db, thread_id)
    if not sandbox.enabled or not sandbox.provider:
        raise RuntimeError(
            f"{protocol} {role} thread has no enabled Eggthreads sandbox"
        )


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
    _git(repository, "config", "user.name", "Egg Git Critic")
    _git(repository, "config", "user.email", "git-critic@entropygradient.ai")


def _initialize_repository(repository: Path) -> None:
    _git(repository, "init", "-b", "main")
    _configure_git(repository)


def _commit(repository: Path, message: str) -> None:
    _configure_git(repository)
    _git(repository, "add", "-A")
    if _git_status(repository):
        _git(repository, "commit", "-m", message)


def _commit_trusted_result(repository: Path, actor_head: str, protocol: str) -> None:
    """Commit one complete Critic result, including its public projection."""

    trusted = repository / ".trusted"
    if trusted.exists():
        _git(repository, "add", "-f", ".trusted")
    _commit(
        repository,
        f"[{protocol.lower()}] trusted Critic result after {actor_head[:12]}",
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


__all__ = ["GitCritic"]
