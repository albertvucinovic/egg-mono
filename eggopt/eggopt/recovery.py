from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from eggflow import ContextLimitExceededError
from eggthreads import continue_thread, load_thread_projection

from .context_limit import full_context_tokens


class InteractionRecoveryError(RuntimeError):
    """A persisted agent interaction could not be reset for retry."""


@dataclass(frozen=True)
class InteractionRecovery:
    """Recover one trigger-anchored Eggthreads interaction before retry.

    This is the Eggopt equivalent of EvolveTropy's ``WaitForLLMResponse``
    recovery policy. If the operation's durable trigger has no usable assistant
    response, Eggthreads' canonical explicit-target continuation replays exactly
    that interaction; whole-thread diagnosis never chooses an older boundary.
    """

    db: Any
    thread_id: str
    trigger_msg_id: str
    context_limit: int | None = None
    operation: str = "agent interaction"

    def __post_init__(self) -> None:
        if not isinstance(self.thread_id, str) or not self.thread_id:
            raise ValueError("thread_id must be a non-empty string")
        if not isinstance(self.trigger_msg_id, str) or not self.trigger_msg_id:
            raise ValueError("trigger_msg_id must be a non-empty string")
        if self.context_limit is not None and (
            isinstance(self.context_limit, bool)
            or not isinstance(self.context_limit, int)
            or self.context_limit < 1
        ):
            raise ValueError("context_limit must be positive or None")
        if not isinstance(self.operation, str) or not self.operation:
            raise ValueError("operation must be a non-empty string")

    def recover(self) -> bool:
        if self.context_limit is not None:
            current = full_context_tokens(self.db, self.thread_id)
            if current >= self.context_limit:
                raise ContextLimitExceededError(
                    f"{self.operation} full context limit reached before recovery; "
                    f"operation terminated ({current} >= {self.context_limit})"
                )

        if _has_usable_answer_after(self.db, self.thread_id, self.trigger_msg_id):
            return True

        # Recovery is scoped to this operation's durable trigger. Never convert
        # broad whole-thread diagnosis into an older implicit rewind boundary.
        result = continue_thread(self.db, self.thread_id, self.trigger_msg_id)
        if not result.success:
            raise InteractionRecoveryError(
                f"Failed to recover {self.operation} on thread {self.thread_id}: "
                f"{result.message}"
            )
        return True


def _has_usable_answer_after(db: Any, thread_id: str, trigger_msg_id: str) -> bool:
    projection = load_thread_projection(db, thread_id)
    trigger = next(
        (
            message
            for message in projection.messages
            if message.msg_id == trigger_msg_id
        ),
        None,
    )
    if trigger is None:
        raise InteractionRecoveryError(
            f"Interaction trigger {trigger_msg_id} is unavailable on thread {thread_id}"
        )
    return any(
        message.created_event_seq > trigger.created_event_seq
        and message.payload.get("role") == "assistant"
        and not message.payload.get("tool_calls")
        for message in projection.messages
    )


__all__ = ["InteractionRecovery", "InteractionRecoveryError"]
