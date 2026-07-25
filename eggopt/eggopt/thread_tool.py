from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from eggflow import Task
from eggthreads import (
    ToolRegistry,
    build_tool_call_states,
    record_synthetic_user_tool_call,
)

from .context import _current_evaluation, _evaluation_runtime
from .identity import canonical_json, digest_payload


@dataclass
class ThreadTool(Task):
    """Run one durable synthetic tool call on an assigned Eggthreads thread."""

    tools: ToolRegistry = field(repr=False, compare=False)
    thread_id: str
    name: str
    arguments: Any
    occurrence: int | None = None
    origin: str = "eggopt"

    def get_cache_key(self) -> str:
        return digest_payload(
            "eggopt.thread-tool.v1",
            {
                "thread": self.thread_id,
                "name": self.name,
                "arguments": json.loads(
                    canonical_json(self.arguments, what="tool arguments")
                ),
                "occurrence": self.occurrence,
                "origin": self.origin,
            },
        )

    async def run(self) -> str:
        runtime_key = str(_current_evaluation()["_runtime_key"])
        db = _evaluation_runtime(runtime_key)
        key = self.get_cache_key()
        call_id = key.rsplit(":", 1)[-1]
        call = build_tool_call_states(db, self.thread_id).get(call_id)
        if call is None:
            output = await self.tools.execute_async(
                self.name,
                self.arguments,
                thread_id=self.thread_id,
                db=db,
                initial_model_key=None,
            )
            record_synthetic_user_tool_call(
                db,
                self.thread_id,
                self.name,
                self.arguments,
                str(output),
                origin=self.origin,
                tool_call_id=call_id,
            )
        elif call.name != self.name or _arguments(call.arguments) != _arguments(
            canonical_json(self.arguments, what="tool arguments")
        ):
            raise RuntimeError("persisted tool call contradicts ThreadTool identity")
        call = build_tool_call_states(db, self.thread_id)[call_id]
        if call.finished_reason != "success" or call.finished_output is None:
            raise RuntimeError(
                f"{self.name} tool call failed: {call.finished_reason or call.state}"
            )
        return call.finished_output


def _arguments(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


__all__ = ["ThreadTool"]
