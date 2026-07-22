from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from eggthreads import (
    RunnerConfig,
    ThreadRunner,
    ThreadsDB,
    ToolRegistry,
    append_message,
    approve_tool_calls_for_thread,
    count_text_tokens,
    create_root_thread,
    edit_message,
    get_thread_sandbox_config,
    get_thread_tools_config,
    get_thread_working_directory,
    interrupt_thread,
    load_thread_projection,
    provider_context_token_stats,
    set_thread_model,
    set_thread_sandbox_config,
    set_thread_tool_allowlist,
    set_thread_tools_enabled,
    set_thread_working_directory,
    thread_state,
)

from ._identity import canonical_json
from .reflection import (
    CandidateMutation,
    CandidateMutations,
    ReflectionConversation,
)

SOLVER_SAFE_PROFILE_NAME = "solver_safe"
SOLVER_SAFE_PROFILE_VERSION = "3"
_MUTATION_REPAIR_POLICY = "eggopt.gepa.strict-mutation-repair"
_MUTATION_REPAIR_VERSION = "1"
SOLVER_SAFE_TOOLS = frozenset(
    {
        "python_exec",
        "python_repl",
        "bash",
        "bash_repl",
        "add_local_file_to_model_context",
        "read_long_tool_output",
        "skill",
        "tool_help",
        "threads",
        "execute_tool_in_other_thread",
    }
)


def configure_solver_safe_tools(
    db: ThreadsDB,
    study_thread_id: str,
    *,
    workspace: str | Path,
) -> Mapping[str, Any]:
    """Apply the versioned solver-safe tool and Docker sandbox profile."""

    root = db.get_thread(study_thread_id)
    if root is None:
        raise ValueError(f"study thread not found: {study_thread_id}")
    parent = db.conn.execute(
        "SELECT parent_id FROM children WHERE child_id=?", (study_thread_id,)
    ).fetchone()
    if parent is not None:
        raise ValueError("solver_safe must be configured on a study root")
    workspace_path = Path(workspace).resolve()
    workspace_path.mkdir(parents=True, exist_ok=True)
    set_thread_working_directory(
        db,
        study_thread_id,
        str(workspace_path),
        reason="eggopt solver_safe workspace",
    )
    set_thread_tools_enabled(db, study_thread_id, True)
    set_thread_tool_allowlist(db, study_thread_id, set(SOLVER_SAFE_TOOLS))
    sandbox_settings = {
        "provider": "docker",
        "network": {"allowedDomains": [], "deniedDomains": []},
        "workspace": "/workspace",
        "filesystem": {
            "allowWrite": ["."],
            "denyWrite": [".egg"],
            "denyRead": [".egg"],
        },
        "extra_mounts": [],
        "extra_args": ["--cap-drop", "ALL"],
    }
    set_thread_sandbox_config(
        db,
        study_thread_id,
        enabled=True,
        provider="docker",
        settings=sandbox_settings,
        user_control_enabled=False,
        reason="eggopt solver_safe sandbox",
    )
    return {
        "profile": SOLVER_SAFE_PROFILE_NAME,
        "version": SOLVER_SAFE_PROFILE_VERSION,
        "tools": sorted(SOLVER_SAFE_TOOLS),
        "sandbox": sandbox_settings,
    }


def create_solver_safe_study(
    db: ThreadsDB,
    *,
    workspace: str | Path,
    model_key: str | None = None,
    models_path: str = "models.json",
    all_models_path: str = "all-models.json",
    name: str = "GEPA Study",
) -> tuple[str, Mapping[str, Any]]:
    """Create the authoritative study root and apply ``solver_safe``."""

    study_thread_id = create_root_thread(db, name=name)
    if model_key is not None:
        set_thread_model(
            db,
            study_thread_id,
            model_key,
            reason="eggopt production reflection model",
            models_path=models_path,
            all_models_path=all_models_path,
        )
    profile = configure_solver_safe_tools(
        db,
        study_thread_id,
        workspace=workspace,
    )
    return study_thread_id, profile


class EggthreadsReflectionDrive:
    """Production reflection drive using normal Eggthreads runner semantics."""

    requires_study_thread = True

    def __init__(
        self,
        *,
        llm: Any,
        tools: ToolRegistry,
        drive_identity: Mapping[str, Any],
        runner_config: RunnerConfig | None = None,
        models_path: str = "models.json",
        all_models_path: str = "all-models.json",
        auto_approve_tools: bool = False,
        max_runner_steps: int = 32,
        max_correction_turns: int = 0,
        context_ceiling_tokens: int | None = None,
    ) -> None:
        if not isinstance(tools, ToolRegistry):
            raise TypeError("tools must be an Eggthreads ToolRegistry")
        self.llm = llm
        self.tools = tools
        self.runner_config = runner_config or RunnerConfig()
        self.models_path = models_path
        self.all_models_path = all_models_path
        self.auto_approve_tools = bool(auto_approve_tools)
        self.max_runner_steps = int(max_runner_steps)
        if self.max_runner_steps < 1:
            raise ValueError("max_runner_steps must be positive")
        if (
            isinstance(max_correction_turns, bool)
            or not isinstance(max_correction_turns, int)
            or max_correction_turns < 0
        ):
            raise ValueError("max_correction_turns must be a non-negative integer")
        if context_ceiling_tokens is not None and (
            isinstance(context_ceiling_tokens, bool)
            or not isinstance(context_ceiling_tokens, int)
            or context_ceiling_tokens < 1
        ):
            raise ValueError(
                "context_ceiling_tokens must be a positive integer or None"
            )
        self.max_correction_turns = max_correction_turns
        self.context_ceiling_tokens = context_ceiling_tokens
        identity = json.loads(canonical_json(drive_identity, what="drive_identity"))
        reserved = {"mutation_repair", "context_ceiling"}.intersection(identity)
        if reserved:
            raise ValueError(
                f"drive_identity uses reserved Eggopt key(s): {sorted(reserved)}"
            )
        self.semantic_identity = {
            **identity,
            "mutation_repair": {
                "policy": _MUTATION_REPAIR_POLICY,
                "version": _MUTATION_REPAIR_VERSION,
                "max_correction_turns": max_correction_turns,
            },
            "context_ceiling": {
                "policy": "eggopt.gepa.streaming-context-ceiling",
                "version": "1",
                "max_tokens": context_ceiling_tokens,
            },
        }

    def validate_study(self, db: ThreadsDB, study_thread_id: str) -> None:
        """Require an explicit, sandboxed, workspace-bounded study root."""

        if db.get_thread(study_thread_id) is None:
            raise ValueError(f"study thread not found: {study_thread_id}")
        root_id = _root_thread_id(db, study_thread_id)
        root_tools_cfg = get_thread_tools_config(db, root_id)
        if root_tools_cfg.policy_error:
            raise ValueError(
                f"study tool policy is unavailable: {root_tools_cfg.policy_error}"
            )
        if root_tools_cfg.allowed_tools is None:
            raise ValueError(
                "production reflection study root requires an explicit tool allowlist"
            )
        if not root_tools_cfg.allowed_tools.issubset(SOLVER_SAFE_TOOLS):
            raise ValueError(
                "production reflection root allowlist exceeds solver_safe"
            )
        tools_cfg = get_thread_tools_config(db, study_thread_id)
        if tools_cfg.policy_error:
            raise ValueError(f"study tool policy is unavailable: {tools_cfg.policy_error}")
        if tools_cfg.allowed_tools is None:
            raise ValueError(
                "production reflection study requires an explicit tool allowlist"
            )
        if not tools_cfg.allowed_tools.issubset(SOLVER_SAFE_TOOLS):
            raise ValueError("production reflection allowlist exceeds solver_safe")
        sandbox = get_thread_sandbox_config(db, study_thread_id)
        _validate_safe_sandbox(sandbox)
        workspace = get_thread_working_directory(db, study_thread_id)
        if not workspace.is_dir():
            raise ValueError("production reflection workspace does not exist")

    async def start(
        self,
        conversation: ReflectionConversation,
        request: Mapping[str, Any],
    ) -> CandidateMutation | CandidateMutations:
        self.validate_study(conversation.db, conversation.thread_id)
        return await self._drive_async(conversation, request)

    async def resume(
        self,
        conversation: ReflectionConversation,
        request: Mapping[str, Any],
    ) -> CandidateMutation | CandidateMutations:
        self.validate_study(conversation.db, conversation.thread_id)
        return await self._drive_async(conversation, request)

    async def _drive_async(
        self,
        conversation: ReflectionConversation,
        request: Mapping[str, Any],
    ) -> CandidateMutation | CandidateMutations:
        db = conversation.db
        thread_id = conversation.thread_id
        after_seq = db.max_event_seq(thread_id)
        if self.auto_approve_tools:
            approve_tool_calls_for_thread(
                db,
                thread_id,
                decision="global_approval",
                reason="Application opted into auto-approval for this reflection drive",
            )
        runner = ThreadRunner(
            db,
            thread_id,
            llm=self.llm,
            config=self.runner_config,
            models_path=self.models_path,
            all_models_path=self.all_models_path,
            tools=self.tools,
        )
        for correction in range(self.max_correction_turns + 1):
            await self._run_until_settled(runner, db, thread_id)
            message = _causal_final_assistant(db, thread_id, after_seq)
            try:
                mutations = _strict_mutations(message.payload.get("content"), request)
            except (TypeError, ValueError) as exc:
                if correction >= self.max_correction_turns:
                    raise ValueError(
                        "reflection mutation envelope remained invalid after "
                        f"{self.max_correction_turns} corrective turn(s): {_repair_reason(exc)}"
                    ) from exc
                repair_feedback = _repair_feedback(exc, request)
                append_message(
                    db,
                    thread_id,
                    "user",
                    repair_feedback,
                    extra={
                        "eggopt_kind": "eggopt.gepa.reflection-repair.v1",
                        "semantic_key": conversation.semantic_key,
                        "repair_policy": _MUTATION_REPAIR_POLICY,
                        "repair_version": _MUTATION_REPAIR_VERSION,
                        "correction_turn": correction + 1,
                        "validation_feedback": repair_feedback,
                    },
                )
                after_seq = db.max_event_seq(thread_id)
                continue
            edit_message(
                db,
                thread_id,
                message.msg_id,
                message.payload.get("content", ""),
                extra={
                    "eggopt_kind": "eggopt.gepa.reflection-response.v1",
                    "semantic_key": conversation.semantic_key,
                    "mutations": [dict(item.updates) for item in mutations],
                },
            )
            conversation.response_message_id = message.msg_id
            return mutations if len(mutations) > 1 else mutations.items[0]
        raise AssertionError("unreachable correction loop")

    async def _run_until_settled(
        self,
        runner: ThreadRunner,
        db: ThreadsDB,
        thread_id: str,
    ) -> None:
        for _ in range(self.max_runner_steps):
            before_seq = db.max_event_seq(thread_id)
            progressed = await self._run_step(runner, db, thread_id)
            state = thread_state(db, thread_id)
            if state == "waiting_tool_approval":
                raise RuntimeError(
                    "reflection tool call requires approval; configure an existing "
                    "approval path or set auto_approve_tools=True"
                )
            if state == "waiting_user":
                terminal_error = _runner_error_after(db, thread_id, before_seq)
                if terminal_error is not None:
                    raise RuntimeError(terminal_error)
                return
            if not progressed:
                raise RuntimeError(f"reflection runner stalled in state {state!r}")
        raise RuntimeError("reflection runner did not settle within max_runner_steps")

    async def continue_for_recovery(
        self,
        conversation: ReflectionConversation,
        request: Mapping[str, Any],
    ) -> CandidateMutation | CandidateMutations:
        """Continue an interrupted turn beyond the normal per-drive step slice."""

        original = self.max_runner_steps
        self.max_runner_steps = max(original, 256)
        try:
            return await self.resume(conversation, request)
        finally:
            self.max_runner_steps = original

    async def _run_step(
        self,
        runner: ThreadRunner,
        db: ThreadsDB,
        thread_id: str,
    ) -> bool:
        if self.context_ceiling_tokens is None:
            return await runner.run_once()
        current = int(
            provider_context_token_stats(db, thread_id).get("context_tokens") or 0
        )
        if current >= self.context_ceiling_tokens:
            raise RuntimeError(
                "reflection context ceiling reached before provider call; "
                f"operation terminated ({current} >= {self.context_ceiling_tokens})"
            )
        task = asyncio.create_task(runner.run_once())
        try:
            while not task.done():
                await asyncio.sleep(0)
                current = int(
                    provider_context_token_stats(db, thread_id).get("context_tokens")
                    or 0
                )
                live = _open_llm_stream_tokens(db, thread_id)
                if current + live >= self.context_ceiling_tokens:
                    interrupt_thread(
                        db,
                        thread_id,
                        reason=(
                            "eggopt reflection context ceiling reached: "
                            f"{current + live} >= {self.context_ceiling_tokens}"
                        ),
                    )
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    raise RuntimeError(
                        "reflection context ceiling reached; operation terminated"
                    )
            return await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


def _root_thread_id(db: ThreadsDB, thread_id: str) -> str:
    current = thread_id
    seen: set[str] = set()
    while current not in seen:
        seen.add(current)
        row = db.conn.execute(
            "SELECT parent_id FROM children WHERE child_id=?", (current,)
        ).fetchone()
        if row is None:
            return current
        current = str(row[0])
    raise ValueError("cycle in reflection thread ancestry")


def _validate_safe_sandbox(config: Any) -> None:
    if not config.enabled or config.provider != "docker":
        raise ValueError("production reflection requires enabled Docker sandboxing")
    settings = dict(config.settings or {})
    network = settings.get("network")
    if not (
        network == "none"
        or isinstance(network, Mapping)
        and network.get("allowedDomains") == []
    ):
        raise ValueError("production reflection sandbox must deny network access")
    if settings.get("workspace") != "/workspace":
        raise ValueError("production reflection sandbox workspace must be /workspace")
    filesystem = settings.get("filesystem")
    if not isinstance(filesystem, Mapping):
        raise ValueError("production reflection sandbox needs filesystem policy")
    if filesystem.get("allowWrite") != ["."]:
        raise ValueError("production reflection writes must be workspace-bounded")
    if ".egg" not in filesystem.get("denyWrite", []):
        raise ValueError("production reflection must deny writes to .egg")


def _causal_final_assistant(db: ThreadsDB, thread_id: str, after_seq: int):
    projection = load_thread_projection(db, thread_id, db.max_event_seq(thread_id))
    candidates = [
        message
        for message in projection.messages
        if message.created_event_seq > after_seq
        and message.payload.get("role") == "assistant"
        and not message.payload.get("tool_calls")
    ]
    if not candidates:
        raise RuntimeError("reflection runner produced no final assistant response")
    return candidates[-1]


def _runner_error_after(db: ThreadsDB, thread_id: str, after_seq: int) -> str | None:
    projection = load_thread_projection(db, thread_id, db.max_event_seq(thread_id))
    for message in reversed(projection.messages):
        if message.created_event_seq <= after_seq:
            break
        if message.payload.get("runner_error"):
            return str(message.payload.get("content") or "reflection runner failed")
    return None


def _open_llm_stream_tokens(db: ThreadsDB, thread_id: str) -> int:
    row = db.current_open(thread_id)
    if row is None or row["purpose"] != "llm":
        return 0
    invoke_id = str(row["invoke_id"])
    parts: list[str] = []
    for event in db.events_since(thread_id, 0):
        if event["invoke_id"] != invoke_id or event["type"] != "stream.delta":
            continue
        try:
            payload = json.loads(event["payload_json"] or "{}")
        except json.JSONDecodeError:
            continue
        for key in ("text", "reason", "reasoning_summary"):
            value = payload.get(key)
            if isinstance(value, str):
                parts.append(value)
        tool_call = payload.get("tool_call")
        if isinstance(tool_call, Mapping):
            parts.append(str(tool_call.get("arguments_delta") or ""))
    return count_text_tokens("".join(parts))


def _repair_reason(exc: BaseException) -> str:
    reason = " ".join(str(exc).split())
    return reason[:300] or "invalid mutation envelope"


def _repair_feedback(exc: BaseException, request: Mapping[str, Any]) -> str:
    components = ", ".join(
        sorted(str(item) for item in request["components_to_update"])
    )
    count = int(request["mutation_count"])
    return (
        "Your previous response could not be accepted: "
        f"{_repair_reason(exc)}. Return only strict JSON with exactly the key "
        f"'mutations', containing {count} object(s) that update only: {components}. "
        "Do not include Markdown or commentary."
    )


def _strict_mutations(content: Any, request: Mapping[str, Any]) -> CandidateMutations:
    if not isinstance(content, str):
        raise ValueError("reflection assistant response must be a JSON string")
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError("reflection assistant response must be strict JSON") from exc
    if not isinstance(payload, Mapping) or set(payload) != {"mutations"}:
        raise ValueError("reflection JSON must contain only 'mutations'")
    raw = payload.get("mutations")
    if not isinstance(raw, list):
        raise ValueError("reflection 'mutations' must be a list")
    mutations = CandidateMutations(
        tuple(CandidateMutation(item) for item in raw)
    )
    expected_count = int(request["mutation_count"])
    if len(mutations) != expected_count:
        raise ValueError(
            f"reflection JSON needs {expected_count} mutation(s), got {len(mutations)}"
        )
    allowed = set(request["components_to_update"])
    for mutation in mutations:
        unexpected = set(mutation.updates) - allowed
        if unexpected:
            raise ValueError(
                f"mutation updated unrequested components: {sorted(unexpected)}"
            )
    return mutations
