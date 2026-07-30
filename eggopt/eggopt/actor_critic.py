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
    approve_tool_calls_for_thread,
    create_child_thread,
    current_thread_model,
    get_thread_auto_approval_status,
    list_children_with_meta,
    load_thread_projection,
    set_thread_model,
    set_thread_sandbox_config,
    set_thread_tool_allowlist,
    set_thread_tools_enabled,
    set_thread_working_directory,
    thread_state,
)

from .context import (
    _current_evaluation,
    _current_evaluation_context_limit,
    _evaluation_runtime,
)
from .context_limit import run_with_full_context_limit
from .identity import canonical_json, digest_payload
from .recovery import InteractionRecovery
from .tools import default_safe_tools, safe_tools

_NO_ANSWER = object()


@dataclass(frozen=True)
class Agent:
    """Small Eggthreads agent configuration with an Eggopt full-history budget."""

    llm: Any = field(repr=False, compare=False)
    identity: Mapping[str, Any]
    tools: ToolRegistry = field(
        default_factory=default_safe_tools, repr=False, compare=False
    )
    model_key: str | None = None
    models_path: str = "models.json"
    runner_config: RunnerConfig = field(
        default_factory=RunnerConfig, repr=False, compare=False
    )
    context_limit: int | None = None
    auto_approve_tools: bool = False
    allowed_tools: frozenset[str] | None = None
    system_prompt: str | None = None

    def __post_init__(self) -> None:
        canonical_json(self.identity, what="agent identity")
        limit = self.context_limit
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit < 1
        ):
            raise ValueError("agent context_limit must be a positive integer or None")
        if self.runner_config.context_limit is not None:
            raise ValueError(
                "runner_config.context_limit is an Eggthreads provider-context limit; "
                "pass Eggopt's full-history context_limit instead"
            )
        tools, allowed = safe_tools(
            self.tools,
            allowed_tools=self.allowed_tools,
        )
        object.__setattr__(self, "tools", tools)
        object.__setattr__(self, "allowed_tools", allowed)
        if self.system_prompt is not None and not self.system_prompt:
            raise ValueError("agent system_prompt must be non-empty or None")

    @property
    def task_identity(self) -> Mapping[str, Any]:
        """Return the durable identity of this agent's execution semantics."""

        return {
            "identity": self.identity,
            "model_key": self.model_key,
            "models_path": self.models_path,
            "allowed_tools": sorted(self.allowed_tools),
            "auto_approve_tools": self.auto_approve_tools,
            "system_prompt": self.system_prompt,
        }


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
    value: Any = _NO_ANSWER

    def __post_init__(self) -> None:
        if self.value is _NO_ANSWER:
            object.__setattr__(self, "value", self.answer)


@dataclass(frozen=True)
class Critique:
    """Typed deterministic-Critic result with an optional extracted value."""

    decision: str
    feedback: str
    value: Any = _NO_ANSWER

    def __post_init__(self) -> None:
        if self.decision not in {"accept", "revise"}:
            raise ValueError("Critique decision must be accept or revise")
        if not isinstance(self.feedback, str):
            raise TypeError("Critique feedback must be a string")

    @classmethod
    def accept(cls, value: Any, feedback: str = "Accepted.") -> Critique:
        return cls("accept", feedback, value)

    @classmethod
    def revise(cls, feedback: str) -> Critique:
        return cls("revise", feedback)


@dataclass
class ActorCritic(Task):
    """Bounded, recoverable Actor → Critic → revision loop.

    Prompt factories may return text directly or a Task whose result is text.
    Task prompts run after the persistent Actor/Critic pair is assigned, so they
    can prepare thread-bound inputs before an agent turn. The returned Task must
    include those inputs in its own durable identity.
    """

    actor: Agent = field(repr=False, compare=False)
    critic: Agent | Task = field(repr=False, compare=False)
    actor_prompt: Callable[[int, Mapping[str, Any]], str | Task] = field(
        repr=False, compare=False
    )
    critic_prompt: Callable[[int, Mapping[str, Any]], str | Task] | None = field(
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
            "actor": self.actor.task_identity,
            "critic": (
                self.critic.task_identity
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
        if self.names is not None:
            identity["names"] = self.names
        return digest_payload("eggopt.actor-critic.v3", identity)

    def recover_interaction(
        self,
        db: Any,
        evaluation_id: str,
        context_limit: int | None,
    ) -> bool:
        names = self.names or ("Actor", "Critic")
        critic_id = _named_child(db, evaluation_id, names[1])
        actor_id = _named_child(db, critic_id, names[0]) if critic_id else None
        recovered = True
        if actor_id is not None:
            recovered = _recover_latest_interaction(
                db,
                actor_id,
                self.actor.context_limit or context_limit,
                f"ActorCritic {names[0]}",
            )
        if isinstance(self.critic, Agent) and critic_id is not None:
            critic_recovered = _recover_latest_interaction(
                db,
                critic_id,
                self.critic.context_limit or context_limit,
                f"ActorCritic {names[1]}",
            )
            recovered = recovered and critic_recovered
        return recovered

    def recover(self) -> bool:
        context = _current_evaluation()
        return self.recover_interaction(
            _evaluation_runtime(str(context["_runtime_key"])),
            str(context["evaluation_thread_id"]),
            _current_evaluation_context_limit(),
        )

    def run(self):
        context = _current_evaluation()
        runtime_key = str(context["_runtime_key"])
        evaluation_id = str(context["evaluation_thread_id"])
        workspace = str(context["inner_context"])
        context_limit = _current_evaluation_context_limit()
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
            prompt = yield from _resolve_prompt(
                self.actor_prompt(round_number, state), "actor"
            )
            answer = yield _AgentTurn(
                runtime_key,
                actor_id,
                self.actor,
                prompt,
                "actor",
                round_number,
                self.actor.context_limit or context_limit,
            )
            state = {**state, "answer": answer}
            if isinstance(self.critic, Agent):
                prompt = yield from _resolve_prompt(
                    self.critic_prompt(round_number, state), "critic"
                )
                raw = yield _AgentTurn(
                    runtime_key,
                    critic_id,
                    self.critic,
                    prompt,
                    "critic",
                    round_number,
                    self.critic.context_limit or context_limit,
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
                    decision.get("value", answer),
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
    runtime_key: str
    evaluation_id: str
    workspace: str
    actor: Agent = field(repr=False, compare=False)
    critic: Agent | Task = field(repr=False, compare=False)
    names: tuple[str, str]

    def get_cache_key(self) -> str:
        identity = {
            "evaluation": self.evaluation_id,
            "actor": self.actor.task_identity,
            "critic": (
                self.critic.task_identity
                if isinstance(self.critic, Agent)
                else _task_identity(self.critic)
            ),
            "names": self.names,
        }
        return digest_payload("eggopt.actor-critic.ensure-pair.v1", identity)

    def run(self):
        db = _evaluation_runtime(self.runtime_key)
        actor_name, critic_name = self.names
        critic_id = _named_child(db, self.evaluation_id, critic_name)
        actor_id = _named_child(db, critic_id, actor_name) if critic_id else None
        legacy_actor = _named_child(db, self.evaluation_id, actor_name)

        if critic_id and actor_id and not legacy_actor:
            pass
        elif critic_id and legacy_actor and not actor_id:
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
        return actor_id, critic_id


def _named_child(db: Any, parent_id: str, name: str) -> str | None:
    return next(
        (
            thread_id
            for thread_id, child_name, *_rest in list_children_with_meta(db, parent_id)
            if child_name == name
        ),
        None,
    )


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
            "agent": self.agent.task_identity,
            "workspace": self.workspace,
            "role": self.role,
        }
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
        if (
            self.agent.model_key is not None
            and current_thread_model(db, self.thread_id) != self.agent.model_key
        ):
            set_thread_model(
                db,
                self.thread_id,
                self.agent.model_key,
                reason=f"ActorCritic {self.role}",
                models_path=self.agent.models_path,
            )
        set_thread_working_directory(
            db,
            self.thread_id,
            self.workspace,
            reason="ActorCritic shared innerContext",
        )
        set_thread_tools_enabled(db, self.thread_id, True)
        set_thread_tool_allowlist(db, self.thread_id, set(self.agent.allowed_tools))
        approved = get_thread_auto_approval_status(db, self.thread_id)
        if self.agent.auto_approve_tools != approved:
            approve_tool_calls_for_thread(
                db,
                self.thread_id,
                decision=(
                    "global_approval"
                    if self.agent.auto_approve_tools
                    else "revoke_global_approval"
                ),
                reason=f"ActorCritic {self.role} agent configuration",
            )
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
    context_limit: int | None = None

    def get_cache_key(self) -> str:
        return digest_payload(
            "eggopt.actor-critic.turn.v1",
            {
                "thread": self.thread_id,
                "agent": self.agent.task_identity,
                "prompt": self.prompt,
                "role": self.role,
                "round": self.round_number,
            },
        )

    async def run(self) -> Any:
        db = _evaluation_runtime(self.runtime_key)
        semantic_key = self.get_cache_key()
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
                db, self.thread_id, _message_event_seq(db, self.thread_id, prompt_id)
            )
            if persisted_answer is not _NO_ANSWER:
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
        await _run_until_waiting(
            runner,
            db,
            self.thread_id,
            after_seq,
            self.context_limit,
        )
        response = _latest_answer(db, self.thread_id, after_seq)
        if response is _NO_ANSWER:
            raise RuntimeError(f"{self.role} produced no final answer")
        return response


async def _run_until_waiting(
    runner: ThreadRunner,
    db: Any,
    thread_id: str,
    after_seq: int,
    context_limit: int | None,
) -> None:
    while True:
        state = thread_state(db, thread_id)
        if state == "waiting_user":
            if _latest_answer(db, thread_id, after_seq) is not _NO_ANSWER:
                return
            raise RuntimeError("ActorCritic agent settled without a final answer")
        progressed = await run_with_full_context_limit(
            runner,
            db,
            thread_id,
            context_limit,
            operation="ActorCritic agent",
        )
        if thread_state(db, thread_id) == "waiting_tool_approval":
            raise RuntimeError("ActorCritic tool call requires approval")
        if not progressed and thread_state(db, thread_id) != "waiting_user":
            raise RuntimeError("ActorCritic agent stalled")


def _critic_decision(value: Any) -> dict[str, Any]:
    if isinstance(value, Critique):
        decision = {"decision": value.decision, "feedback": value.feedback}
        if value.value is not _NO_ANSWER:
            decision["value"] = value.value
    elif isinstance(value, Mapping):
        decision = dict(value)
    elif not isinstance(value, str):
        raise ValueError("Critic answer must be strict JSON text")  # noqa: TRY004
    else:
        try:
            decision = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("Critic answer must be strict JSON") from exc
    expected = {"decision", "feedback"}
    if (
        not isinstance(decision, dict)
        or not expected <= set(decision)
        or set(decision) - (expected | {"value"})
    ):
        raise ValueError(
            "Critic JSON must contain decision, feedback, and optional value"
        )
    if decision["decision"] not in {"accept", "revise"}:
        raise ValueError("Critic decision must be accept or revise")
    if not isinstance(decision["feedback"], str):
        raise ValueError("Critic feedback must be a string")  # noqa: TRY004
    result = {
        "decision": str(decision["decision"]),
        "feedback": decision["feedback"],
    }
    if "value" in decision:
        result["value"] = decision["value"]
    return result


def _resolve_prompt(value: Any, role: str):
    """Resolve one post-assignment prompt without hiding its Eggflow dependency."""

    if isinstance(value, Task):
        value = yield value
    if not isinstance(value, str):
        raise TypeError(f"ActorCritic {role} prompt must resolve to a string")
    return value


def _prompt_message_id(db: Any, thread_id: str, semantic_key: str) -> str | None:
    projection = load_thread_projection(db, thread_id)
    message = projection.latest_message(
        metadata_key="eggopt_actor_critic_key",
        metadata_value=semantic_key,
    )
    return message.msg_id if message is not None else None


def _recover_latest_interaction(
    db: Any,
    thread_id: str,
    context_limit: int | None,
    operation: str,
) -> bool:
    projection = load_thread_projection(db, thread_id)
    message = projection.latest_message(
        role="user",
        metadata_key="eggopt_actor_critic_key",
    )
    if message is None:
        return True
    return InteractionRecovery(
        db,
        thread_id,
        message.msg_id,
        context_limit,
        operation,
    ).recover()


def _prompt_event_seq(db: Any, thread_id: str, semantic_key: str) -> int:
    projection = load_thread_projection(db, thread_id)
    message = projection.latest_message(
        metadata_key="eggopt_actor_critic_key",
        metadata_value=semantic_key,
    )
    if message is None:
        raise RuntimeError("ActorCritic prompt was not persisted")
    return message.created_event_seq


def _message_event_seq(db: Any, thread_id: str, message_id: str) -> int:
    projection = load_thread_projection(db, thread_id)
    message = projection.message(message_id)
    if message is None:
        raise RuntimeError("ActorCritic prompt event is unavailable")
    return message.created_event_seq


def _answer_after_message(db: Any, thread_id: str, after_seq: int) -> Any:
    return _latest_answer(db, thread_id, after_seq)


def _latest_answer(db: Any, thread_id: str, after_seq: int) -> Any:
    """Return this user turn's answer, never a later turn's response."""

    projection = load_thread_projection(db, thread_id)
    next_user_seq = next(
        (
            message.created_event_seq
            for message in projection.messages
            if message.created_event_seq > after_seq
            and message.payload.get("role") == "user"
        ),
        None,
    )
    answers = [
        message
        for message in projection.messages
        if message.created_event_seq > after_seq
        and (next_user_seq is None or message.created_event_seq < next_user_seq)
        and message.payload.get("role") == "assistant"
        and not message.payload.get("tool_calls")
    ]
    return answers[-1].payload.get("content") if answers else _NO_ANSWER


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


__all__ = ["ActorCritic", "ActorCriticResult", "Agent", "Critique"]
