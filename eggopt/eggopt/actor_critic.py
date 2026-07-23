from __future__ import annotations

import copy
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from eggflow import Task, keyed
from eggthreads import (
    RunnerConfig,
    ThreadRunner,
    ToolRegistry,
    append_message,
    create_child_thread,
    load_thread_projection,
    set_thread_tool_allowlist,
    set_thread_tools_enabled,
    set_thread_sandbox_config,
    set_thread_working_directory,
    thread_state,
)

from ._context import _current_evaluation, _evaluation_runtime
from ._identity import canonical_json, digest_payload
from .gepa.production_drive import default_solver_safe_tools, solver_safe_tools


@dataclass(frozen=True)
class Agent:
    """Small Eggthreads agent configuration for ActorCritic."""

    llm: Any = field(repr=False, compare=False)
    identity: Mapping[str, Any]
    tools: ToolRegistry = field(
        default_factory=default_solver_safe_tools, repr=False, compare=False
    )
    model_key: str | None = None
    models_path: str = "models.json"
    runner_config: RunnerConfig = field(
        default_factory=RunnerConfig, repr=False, compare=False
    )
    allowed_tools: frozenset[str] | None = None
    system_prompt: str | None = None

    def __post_init__(self) -> None:
        canonical_json(self.identity, what="agent identity")
        tools, allowed = solver_safe_tools(
            self.tools,
            allowed_tools=self.allowed_tools,
        )
        object.__setattr__(self, "tools", tools)
        object.__setattr__(self, "allowed_tools", allowed)
        if self.system_prompt is not None and not self.system_prompt:
            raise ValueError("agent system_prompt must be non-empty or None")


@dataclass(frozen=True)
class ActorCriticResult:
    answer: Any
    accepted: bool
    feedback: str
    evaluation_thread_id: str
    actor_thread_id: str
    critic_thread_id: str
    workspace: str
    rounds: int


@dataclass
class ActorCritic(Task):
    """Bounded, recoverable Actor → Critic → revision loop."""

    actor: Agent = field(repr=False, compare=False)
    critic: Agent | Task = field(repr=False, compare=False)
    actor_prompt: Callable[[int, Mapping[str, Any]], str] = field(
        repr=False, compare=False
    )
    critic_prompt: Callable[[int, Mapping[str, Any]], str] | None = field(
        default=None, repr=False, compare=False
    )
    max_rounds: int = 3
    names: tuple[str, str] | None = None

    def __post_init__(self) -> None:
        if isinstance(self.max_rounds, bool) or self.max_rounds < 1:
            raise ValueError("max_rounds must be positive")
        if isinstance(self.critic, Agent) and not callable(self.critic_prompt):
            raise TypeError("Agent critic requires critic_prompt")
        if isinstance(self.critic, Task) and self.critic_prompt is not None:
            raise TypeError("Task critic does not use critic_prompt")
        names = self.names or ("Actor", "Critic")
        if any(not isinstance(name, str) or not name.strip() for name in names) or (
            len(names) != 2 or names[0] == names[1]
        ):
            raise ValueError("ActorCritic names must be two distinct non-empty strings")

    def get_cache_key(self) -> str:
        context = _current_evaluation()
        identity = {
            "evaluation": context["_evaluation_key"],
            "actor": self.actor.identity,
            "critic": (
                self.critic.identity
                if isinstance(self.critic, Agent)
                else _task_identity(self.critic)
            ),
            "actor_prompt": _callable_identity(self.actor_prompt),
            "critic_prompt": (
                _callable_identity(self.critic_prompt)
                if self.critic_prompt is not None
                else None
            ),
            "max_rounds": self.max_rounds,
        }
        if self.actor.system_prompt is not None:
            identity["actor_system_prompt"] = self.actor.system_prompt
        if isinstance(self.critic, Agent) and self.critic.system_prompt is not None:
            identity["critic_system_prompt"] = self.critic.system_prompt
        if self.names is not None:
            identity["names"] = self.names
        return digest_payload("eggopt.actor-critic.v1", identity)

    def run(self):
        context = _current_evaluation()
        runtime_key = str(context["_runtime_key"])
        evaluation_id = str(context["evaluation_thread_id"])
        workspace = str(context["inner_context"])
        actor_id, critic_id = yield _EnsurePair(
            runtime_key,
            evaluation_id,
            workspace,
            self.actor,
            self.critic,
            self.names or ("Actor", "Critic"),
        )
        feedback = ""
        answer: Any = None
        for round_number in range(1, self.max_rounds + 1):
            state = {
                "answer": answer,
                "feedback": feedback,
                "evaluation_thread_id": evaluation_id,
                "actor_thread_id": actor_id,
                "critic_thread_id": critic_id,
                "workspace": workspace,
            }
            answer = yield _AgentTurn(
                runtime_key,
                actor_id,
                self.actor,
                self.actor_prompt(round_number, state),
                "actor",
                round_number,
            )
            state = {**state, "answer": answer}
            if isinstance(self.critic, Agent):
                raw = yield _AgentTurn(
                    runtime_key,
                    critic_id,
                    self.critic,
                    self.critic_prompt(round_number, state),
                    "critic",
                    round_number,
                )
            else:
                raw = yield _TaskCritique(self.critic, round_number, state)
            decision = _critic_decision(raw)
            feedback = decision["feedback"]
            if decision["decision"] == "accept":
                return ActorCriticResult(
                    answer,
                    True,
                    feedback,
                    evaluation_id,
                    actor_id,
                    critic_id,
                    workspace,
                    round_number,
                )
        return ActorCriticResult(
            answer,
            False,
            feedback,
            evaluation_id,
            actor_id,
            critic_id,
            workspace,
            self.max_rounds,
        )


@dataclass
class _EnsurePair(Task):
    cacheable = False

    runtime_key: str
    evaluation_id: str
    workspace: str
    actor: Agent = field(repr=False, compare=False)
    critic: Agent | Task = field(repr=False, compare=False)
    names: tuple[str, str]

    def get_cache_key(self) -> str:
        identity = {
            "evaluation": self.evaluation_id,
            "actor": self.actor.identity,
            "critic": (
                self.critic.identity
                if isinstance(self.critic, Agent)
                else _task_identity(self.critic)
            ),
            "names": self.names,
        }
        if self.actor.system_prompt is not None:
            identity["actor_system_prompt"] = self.actor.system_prompt
        return digest_payload("eggopt.actor-critic.ensure-pair.v1", identity)

    def run(self):
        db = _evaluation_runtime(self.runtime_key)
        persisted = _persisted_pair(db, self.evaluation_id, self.get_cache_key())
        if persisted is not None:
            return persisted

        actor_name, critic_name = self.names
        critic_id = _named_child(db, self.evaluation_id, critic_name)
        actor_id = _named_child(db, critic_id, actor_name) if critic_id else None
        legacy_actor = _named_child(db, self.evaluation_id, actor_name)

        if critic_id and actor_id:
            pass
        elif critic_id and legacy_actor:
            # Replay pairs created by the former sibling topology.
            actor_id = legacy_actor
        elif critic_id or actor_id or legacy_actor:
            raise RuntimeError("ActorCritic evaluation has an incomplete thread pair")
        else:
            critic_id = create_child_thread(
                db,
                self.evaluation_id,
                name=critic_name,
                initial_model_key=(
                    self.critic.model_key if isinstance(self.critic, Agent) else None
                ),
                models_path=(
                    self.critic.models_path
                    if isinstance(self.critic, Agent)
                    else self.actor.models_path
                ),
            )
            if isinstance(self.critic, Agent):
                yield _ConfigureAgent(
                    self.runtime_key,
                    critic_id,
                    self.workspace,
                    self.critic,
                    critic_name,
                )
            else:
                yield _ConfigureWorkspace(
                    self.runtime_key, critic_id, self.workspace, critic_name
                )
            actor_id = create_child_thread(
                db,
                critic_id,
                name=actor_name,
                initial_model_key=self.actor.model_key,
                models_path=self.actor.models_path,
            )

        yield _ConfigureAgent(
            self.runtime_key, actor_id, self.workspace, self.actor, actor_name
        )
        if isinstance(self.critic, Agent):
            yield _ConfigureAgent(
                self.runtime_key,
                critic_id,
                self.workspace,
                self.critic,
                critic_name,
            )
        else:
            yield _ConfigureWorkspace(
                self.runtime_key, critic_id, self.workspace, critic_name
            )
        db.append_event(
            event_id=digest_payload(
                "eggopt.actor-critic.pair.v1", self.get_cache_key()
            ),
            thread_id=self.evaluation_id,
            type_="eggopt.actor-critic.pair.v1",
            payload={
                "semantic_key": self.get_cache_key(),
                "actor_thread_id": actor_id,
                "critic_thread_id": critic_id,
            },
        )
        return actor_id, critic_id


def _named_child(db: Any, parent_id: str, name: str) -> str | None:
    row = db.conn.execute(
        "SELECT children.child_id FROM children JOIN threads "
        "ON threads.thread_id=children.child_id "
        "WHERE children.parent_id=? AND threads.name=? ORDER BY threads.created_at LIMIT 1",
        (parent_id, name),
    ).fetchone()
    return str(row[0]) if row else None


@dataclass
class _ConfigureAgent(Task):
    runtime_key: str
    thread_id: str
    workspace: str
    agent: Agent = field(repr=False, compare=False)
    role: str

    def get_cache_key(self) -> str:
        # v2 makes the full solver-safe default part of durable configuration.
        identity = {
            "thread": self.thread_id,
            "agent": self.agent.identity,
            "workspace": self.workspace,
            "allowed_tools": sorted(self.agent.allowed_tools),
            "role": self.role,
        }
        if self.agent.system_prompt is not None:
            identity["system_prompt"] = self.agent.system_prompt
        return digest_payload("eggopt.actor-critic.configure-agent.v2", identity)

    def run(self) -> None:
        db = _evaluation_runtime(self.runtime_key)
        Path(self.workspace).mkdir(parents=True, exist_ok=True)
        try:
            Path(self.workspace).resolve().relative_to(Path.cwd().resolve())
        except ValueError as exc:
            raise ValueError(
                "ActorCritic run_dir must be inside the current project directory"
            ) from exc
        set_thread_working_directory(
            db,
            self.thread_id,
            self.workspace,
            reason="ActorCritic shared innerContext",
        )
        set_thread_tools_enabled(db, self.thread_id, True)
        set_thread_tool_allowlist(db, self.thread_id, set(self.agent.allowed_tools))
        set_thread_sandbox_config(
            db,
            self.thread_id,
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
            reason="ActorCritic innerContext isolation",
        )
        if self.agent.system_prompt is not None:
            semantic_key = digest_payload(
                "eggopt.actor-critic.system.v1",
                {"thread": self.thread_id, "content": self.agent.system_prompt},
            )
            if _prompt_message_id(db, self.thread_id, semantic_key) is None:
                append_message(
                    db,
                    self.thread_id,
                    "system",
                    self.agent.system_prompt,
                    extra={"eggopt_actor_critic_key": semantic_key},
                )


@dataclass
class _ConfigureWorkspace(Task):
    runtime_key: str
    thread_id: str
    workspace: str
    role: str

    def run(self) -> None:
        db = _evaluation_runtime(self.runtime_key)
        Path(self.workspace).mkdir(parents=True, exist_ok=True)
        Path(self.workspace).resolve().relative_to(Path.cwd().resolve())
        set_thread_working_directory(
            db, self.thread_id, self.workspace, reason=f"ActorCritic {self.role}"
        )


@dataclass
class _TaskCritique(Task):
    critic: Task = field(repr=False, compare=False)
    round_number: int
    state: Mapping[str, Any]

    def get_cache_key(self) -> str:
        return digest_payload(
            "eggopt.actor-critic.task-critique.v1",
            {
                "critic": _task_identity(self.critic),
                "round": self.round_number,
                "answer": self.state["answer"],
                "feedback": self.state["feedback"],
                "critic_thread_id": self.state["critic_thread_id"],
            },
        )

    def run(self):
        state = {**self.state, "round": self.round_number}
        critic = _bind_critic(copy.copy(self.critic), state)
        return (yield keyed(critic, self.get_cache_key()))


@dataclass
class _AgentTurn(Task):
    cacheable = False

    runtime_key: str
    thread_id: str
    agent: Agent = field(repr=False, compare=False)
    prompt: str
    role: str
    round_number: int

    def get_cache_key(self) -> str:
        return digest_payload(
            "eggopt.actor-critic.turn.v1",
            {
                "thread": self.thread_id,
                "agent": self.agent.identity,
                "prompt": self.prompt,
                "role": self.role,
                "round": self.round_number,
            },
        )

    async def run(self) -> Any:
        db = _evaluation_runtime(self.runtime_key)
        semantic_key = self.get_cache_key()
        response = _persisted_response(db, self.thread_id, semantic_key)
        if response is not None:
            return response
        prompt_id = _prompt_message_id(db, self.thread_id, semantic_key)
        if prompt_id is None:
            append_message(
                db,
                self.thread_id,
                "user",
                self.prompt,
                extra={"eggopt_actor_critic_key": semantic_key},
            )
        else:
            persisted_answer = _answer_after_message(
                db, self.thread_id, _message_event_seq(db, prompt_id)
            )
            if persisted_answer is not None:
                _record_answer(db, self.thread_id, semantic_key, persisted_answer)
                return persisted_answer
        after_seq = _prompt_event_seq(db, self.thread_id, semantic_key)
        runner = ThreadRunner(
            db,
            self.thread_id,
            llm=self.agent.llm,
            config=self.agent.runner_config,
            models_path=self.agent.models_path,
            tools=self.agent.tools,
        )
        await _run_until_waiting(runner, db, self.thread_id, after_seq)
        response = _latest_answer(db, self.thread_id, after_seq)
        if response is None:
            raise RuntimeError(f"{self.role} produced no final answer")
        _record_answer(db, self.thread_id, semantic_key, response)
        return response


async def _run_until_waiting(
    runner: ThreadRunner, db: Any, thread_id: str, after_seq: int
) -> None:
    for _ in range(256):
        state = thread_state(db, thread_id)
        if state == "waiting_user":
            if _latest_answer(db, thread_id, after_seq) is not None:
                return
            raise RuntimeError("ActorCritic agent settled without a final answer")
        progressed = await runner.run_once()
        if thread_state(db, thread_id) == "waiting_tool_approval":
            raise RuntimeError("ActorCritic tool call requires approval")
        if not progressed and thread_state(db, thread_id) != "waiting_user":
            raise RuntimeError("ActorCritic agent stalled")
    raise RuntimeError("ActorCritic agent did not settle")


def _critic_decision(value: Any) -> dict[str, str]:
    if isinstance(value, Mapping):
        decision = dict(value)
    elif not isinstance(value, str):
        raise ValueError("Critic answer must be strict JSON text")
    else:
        try:
            decision = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("Critic answer must be strict JSON") from exc
    if not isinstance(decision, dict) or set(decision) != {"decision", "feedback"}:
        raise ValueError("Critic JSON must contain only decision and feedback")
    if decision["decision"] not in {"accept", "revise"}:
        raise ValueError("Critic decision must be accept or revise")
    if not isinstance(decision["feedback"], str):
        raise ValueError("Critic feedback must be a string")
    return {
        "decision": str(decision["decision"]),
        "feedback": decision["feedback"],
    }


def _persisted_response(db: Any, thread_id: str, semantic_key: str) -> Any | None:
    row = db.conn.execute(
        "SELECT json_extract(payload_json, '$.answer') FROM events WHERE thread_id=? "
        "AND type='eggopt.actor-critic.answer.v1' "
        "AND json_extract(payload_json, '$.semantic_key')=? ORDER BY event_seq DESC LIMIT 1",
        (thread_id, semantic_key),
    ).fetchone()
    return json.loads(row[0]) if row and row[0] is not None else None


def _persisted_pair(
    db: Any, evaluation_id: str, semantic_key: str
) -> tuple[str, str] | None:
    row = db.conn.execute(
        "SELECT json_extract(payload_json, '$.actor_thread_id'), "
        "json_extract(payload_json, '$.critic_thread_id') FROM events "
        "WHERE thread_id=? AND type='eggopt.actor-critic.pair.v1' "
        "AND json_extract(payload_json, '$.semantic_key')=? "
        "ORDER BY event_seq DESC LIMIT 1",
        (evaluation_id, semantic_key),
    ).fetchone()
    return (str(row[0]), str(row[1])) if row and row[0] and row[1] else None


def _prompt_message_id(db: Any, thread_id: str, semantic_key: str) -> str | None:
    row = db.conn.execute(
        "SELECT msg_id FROM events WHERE thread_id=? AND type='msg.create' "
        "AND json_extract(payload_json, '$.eggopt_actor_critic_key')=? "
        "ORDER BY event_seq DESC LIMIT 1",
        (thread_id, semantic_key),
    ).fetchone()
    return str(row[0]) if row and row[0] else None


def _prompt_event_seq(db: Any, thread_id: str, semantic_key: str) -> int:
    row = db.conn.execute(
        "SELECT event_seq FROM events WHERE thread_id=? AND type='msg.create' "
        "AND json_extract(payload_json, '$.eggopt_actor_critic_key')=? "
        "ORDER BY event_seq DESC LIMIT 1",
        (thread_id, semantic_key),
    ).fetchone()
    if row is None:
        raise RuntimeError("ActorCritic prompt was not persisted")
    return int(row[0])


def _message_event_seq(db: Any, message_id: str) -> int:
    row = db.conn.execute(
        "SELECT event_seq FROM events WHERE msg_id=? AND type='msg.create' "
        "ORDER BY event_seq DESC LIMIT 1",
        (message_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError("ActorCritic prompt event is unavailable")
    return int(row[0])


def _answer_after_message(db: Any, thread_id: str, after_seq: int) -> Any | None:
    return _latest_answer(db, thread_id, after_seq)


def _record_answer(db: Any, thread_id: str, semantic_key: str, answer: Any) -> None:
    db.append_event(
        event_id=digest_payload("eggopt.actor-critic.answer.v1", semantic_key),
        thread_id=thread_id,
        type_="eggopt.actor-critic.answer.v1",
        payload={"semantic_key": semantic_key, "answer": answer},
    )


def _latest_answer(db: Any, thread_id: str, after_seq: int) -> Any | None:
    projection = load_thread_projection(db, thread_id, db.max_event_seq(thread_id))
    answers = [
        message.payload.get("content")
        for message in projection.messages
        if message.created_event_seq > after_seq
        and message.payload.get("role") == "assistant"
        and not message.payload.get("tool_calls")
    ]
    return answers[-1] if answers else None


def _callable_identity(function: Any) -> Mapping[str, str]:
    return {
        "module": getattr(function, "__module__", ""),
        "name": getattr(function, "__qualname__", function.__class__.__qualname__),
    }


def _task_identity(task: Task) -> Mapping[str, str]:
    return {
        "module": task.__class__.__module__,
        "name": task.__class__.__qualname__,
        "key": task.get_cache_key(),
    }


def _bind_critic(task: Task, state: Mapping[str, Any]) -> Task:
    values = {
        "actor_thread_id": state["actor_thread_id"],
        "critic_thread_id": state["critic_thread_id"],
        "workspace": state["workspace"],
        "answer": state.get("answer"),
        "feedback": state.get("feedback"),
        "round_number": state.get("round"),
    }
    fields = getattr(task, "__dataclass_fields__", {})
    for name, value in values.items():
        if name in fields:
            object.__setattr__(task, name, value)
    return task


__all__ = ["ActorCritic", "ActorCriticResult", "Agent"]
