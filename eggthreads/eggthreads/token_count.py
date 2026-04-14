from __future__ import annotations

"""Approximate token counting utilities for eggthreads.

This module computes *approximate* token statistics for a thread
snapshot using the ``tiktoken`` ``cl100k_base`` encoding when
available, falling back to a simple character‑based heuristic when
``tiktoken`` is not installed.

Design goals
------------

* Keep all token accounting logic inside :mod:`eggthreads` so that
  UIs (Egg TUI, headless schedulers, etc.) share a single definition
  of "context length" and "API usage".
* Operate purely on the cached thread snapshot (``threads.snapshot_json``)
  rather than re‑scanning events.  Snapshot construction is already
  the place where we pay the cost of walking the event log, so this
  keeps token counting cheap at read time.
* Be deliberately approximate:
  - We treat every model as using ``cl100k_base``.
  - We count only actual string content (message ``content``,
    ``reasoning`` / ``reasoning_content``, and serialized
    ``tool_calls``) and ignore protocol overhead.
  - We infer API turns from the *sequence of messages* in the
    snapshot: every assistant message is treated as the result of one
    LLM call whose input is all earlier, non‑``no_api`` messages.
  - Cached-input is a heuristic. We estimate cached input tokens by
    comparing the current call to the most recent prior call for the
    *same model key* (so modelA → modelB → modelA can still count
    caching when providers keep per-model KV caches warm).

Public API
----------

``snapshot_token_stats(snapshot: dict) -> dict``
    Given a snapshot dict of the form produced by
    :class:`eggthreads.snapshot.SnapshotBuilder`, return a
    ``token_stats`` structure that can be embedded back into the
    snapshot under the ``"token_stats"`` key and consumed by UIs.

The returned structure has the shape::

    {
      "per_message": {
        "<msg_id>": {
          "index": 0,
          "role": "assistant",
          "content_tokens": 42,
          "reasoning_tokens": 10,
          "tool_calls_tokens": 5,
          "total_tokens": 57,
        },
        ...
      },
      "context_tokens": 1234,           # approx. length of full context
      "api_usage": {
        "total_input_tokens": 4567,     # sum over all inferred LLM calls
        "total_output_tokens": 890,     # sum over assistant messages
        "cached_tokens": 321,           # approx. context size of last call
        "approx_call_count": 3,
      },
    }

The structure is intentionally minimal so that it can be stored inside
``snapshot_json`` without significantly increasing its size while still
being rich enough for UIs to display per‑message and per‑thread token
information.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import time
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING


if TYPE_CHECKING:  # pragma: no cover
    from .db import ThreadsDB


try:  # Optional dependency; we fall back gracefully if missing.
    import tiktoken  # type: ignore
except Exception:  # pragma: no cover - environment dependent
    tiktoken = None  # type: ignore


_ENCODING_NAME = "cl100k_base"
_encoding = None  # type: ignore[var-annotated]


def _get_encoding():
    """Return a cached tiktoken encoding for cl100k_base, or ``None``.

    We keep this lazy so that importing :mod:`eggthreads` does not
    require ``tiktoken``; only callers that actually request token
    statistics pay the dependency cost.
    """

    global _encoding
    if _encoding is not None:
        return _encoding
    if tiktoken is None:  # pragma: no cover - depends on environment
        _encoding = None
        return _encoding
    try:
        _encoding = tiktoken.get_encoding(_ENCODING_NAME)
    except Exception:  # pragma: no cover - defensive
        _encoding = None
    return _encoding


def _count_text_tokens(text: str) -> int:
    """Approximate token count for a string.

    * If ``tiktoken`` / ``cl100k_base`` is available, we use it
      directly.
    * Otherwise we fall back to a simple ``len(text) // 4`` heuristic,
      which is good enough for high‑level estimates.
    """

    if not isinstance(text, str) or not text:
        return 0

    enc = _get_encoding()
    if enc is None:  # pragma: no cover - depends on environment
        # Rough average of 4 characters per token for English‑ish text.
        # Add 1 for non‑empty strings to avoid zero counts.
        return max(1, len(text) // 4)
    try:
        return len(enc.encode(text))
    except Exception:  # pragma: no cover - extremely defensive
        return max(1, len(text) // 4)


def count_text_tokens(text: str) -> int:
    """Public wrapper for the shared approximate text-token heuristic.

    UIs use this to derive live metrics (for example tokens/second while an
    LLM response is streaming) without duplicating token-counting logic.
    """

    return _count_text_tokens(text)


def _event_ts_to_epoch(ts_value: Any) -> Optional[float]:
    """Parse an event timestamp into epoch seconds."""
    if not ts_value:
        return None
    s = str(ts_value)
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
            return float(dt.timestamp())
        except Exception:
            continue
    return None


def _tps_from_tokens(tokens: int, start_ts: Optional[float], end_ts: Optional[float] = None) -> Optional[float]:
    """Return tokens/second for a token count and time interval."""
    if tokens <= 0 or start_ts is None:
        return None
    if end_ts is None:
        end_ts = time.time()
    try:
        elapsed = float(end_ts) - float(start_ts)
    except Exception:
        return None
    if elapsed <= 0.25:
        return None
    return float(tokens) / float(elapsed)


def llm_message_tps_for_invoke(
    db: "ThreadsDB",
    invoke_id: str,
    *,
    content: str = "",
    reasoning: str = "",
    tool_calls: Optional[List[Dict[str, Any]]] = None,
    end_ts: Optional[float] = None,
) -> Optional[float]:
    """Approximate TPS for a completed assistant message produced by an invoke."""
    if not isinstance(invoke_id, str) or not invoke_id:
        return None
    msg = {
        "role": "assistant",
        "content": content or "",
        "reasoning": reasoning or "",
    }
    if isinstance(tool_calls, list) and tool_calls:
        msg["tool_calls"] = tool_calls
    tokens = int(_tokens_for_message(msg, 0).total_tokens)
    if tokens <= 0:
        return None
    try:
        row = db.conn.execute(
            "SELECT ts FROM events WHERE invoke_id=? AND type='stream.open' ORDER BY event_seq ASC LIMIT 1",
            (invoke_id,),
        ).fetchone()
    except Exception:
        row = None
    start_ts = _event_ts_to_epoch(row[0]) if row is not None else None
    return _tps_from_tokens(tokens, start_ts, end_ts=end_ts)


def live_llm_tps_for_invoke(
    db: "ThreadsDB",
    invoke_id: str,
    *,
    end_ts: Optional[float] = None,
) -> Optional[float]:
    """Approximate live TPS for an in-progress LLM invoke."""
    if not isinstance(invoke_id, str) or not invoke_id:
        return None
    try:
        row = db.conn.execute(
            "SELECT ts FROM events WHERE invoke_id=? AND type='stream.open' ORDER BY event_seq ASC LIMIT 1",
            (invoke_id,),
        ).fetchone()
    except Exception:
        row = None
    start_ts = _event_ts_to_epoch(row[0]) if row is not None else None
    if start_ts is None:
        return None

    content_parts: List[str] = []
    reasoning_parts: List[str] = []
    tool_call_args: Dict[str, str] = {}
    try:
        cur = db.conn.execute(
            "SELECT payload_json FROM events WHERE invoke_id=? AND type='stream.delta' ORDER BY event_seq ASC",
            (invoke_id,),
        )
        rows = cur.fetchall()
    except Exception:
        rows = []

    for (payload_json,) in rows:
        try:
            payload = json.loads(payload_json) if isinstance(payload_json, str) else (payload_json or {})
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            continue
        txt = payload.get("text")
        if isinstance(txt, str) and txt:
            content_parts.append(txt)
        rs = payload.get("reason")
        if isinstance(rs, str) and rs:
            reasoning_parts.append(rs)
        tc = payload.get("tool_call")
        if isinstance(tc, dict):
            tcid = str(tc.get("id") or tc.get("name") or "")
            delta = tc.get("arguments_delta") or tc.get("text")
            if tcid and isinstance(delta, str) and delta:
                tool_call_args[tcid] = tool_call_args.get(tcid, "") + delta

    msg = {
        "role": "assistant",
        "content": "".join(content_parts),
        "reasoning": "".join(reasoning_parts),
    }
    if tool_call_args:
        msg["tool_calls_delta"] = [
            {"id": tcid, "function": {"arguments": args}}
            for tcid, args in tool_call_args.items()
        ]
    tokens = int(_tokens_for_message(msg, 0).total_tokens)

    return _tps_from_tokens(tokens, start_ts, end_ts=end_ts)


def tool_message_tps_for_call(
    db: "ThreadsDB",
    thread_id: str,
    tool_call_id: str,
    *,
    content: str = "",
    end_ts: Optional[float] = None,
) -> Optional[float]:
    """Approximate TPS for a published tool message tied to one tool call."""
    if not isinstance(thread_id, str) or not thread_id or not isinstance(tool_call_id, str) or not tool_call_id:
        return None
    tokens = int(count_text_tokens(content or ""))
    if tokens <= 0:
        return None

    start_ts: Optional[float] = None
    finish_ts: Optional[float] = None
    try:
        cur = db.conn.execute(
            "SELECT type, ts, payload_json FROM events "
            "WHERE thread_id=? AND type IN ('tool_call.execution_started', 'tool_call.finished') ORDER BY event_seq ASC",
            (thread_id,),
        )
        rows = cur.fetchall()
    except Exception:
        rows = []

    for ev_type, ts_value, payload_json in rows:
        try:
            payload = json.loads(payload_json) if isinstance(payload_json, str) else (payload_json or {})
        except Exception:
            payload = {}
        if not isinstance(payload, dict) or payload.get("tool_call_id") != tool_call_id:
            continue
        epoch = _event_ts_to_epoch(ts_value)
        if ev_type == "tool_call.execution_started" and start_ts is None:
            start_ts = epoch
        elif ev_type == "tool_call.finished":
            finish_ts = epoch

    if finish_ts is None:
        finish_ts = end_ts
    return _tps_from_tokens(tokens, start_ts, end_ts=finish_ts)


@dataclass
class _PerMessageTokens:
    index: int
    role: str
    content_tokens: int
    reasoning_tokens: int
    tool_calls_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.content_tokens + self.reasoning_tokens + self.tool_calls_tokens


def _tokens_for_message(msg: Dict[str, Any], index: int) -> _PerMessageTokens:
    """Return token counts for a single snapshot message.

    We only look at fields that are relevant for provider calls:
    ``content``, ``reasoning`` / ``reasoning_content``, and
    ``tool_calls``.  Other metadata (model_key, ids, flags) is ignored
    for token purposes.
    """

    role = str(msg.get("role") or "")

    content = msg.get("content") if isinstance(msg.get("content"), str) else ""
    content_tokens = _count_text_tokens(content)

    reasoning = msg.get("reasoning") or msg.get("reasoning_content")
    if isinstance(reasoning, str):
        reasoning_tokens = _count_text_tokens(reasoning)
    else:
        reasoning_tokens = 0

    tool_calls_tokens = 0
    tcs = msg.get("tool_calls")
    # Normal (snapshot) case: OpenAI-style tool_calls list.
    if isinstance(tcs, list) and tcs:
        try:
            # Count the full serialized tool_calls structure, including
            # structural fields (ids, type, etc.), so that this number
            # more closely approximates what a provider will charge
            # for.  This intentionally over-approximates slightly but
            # stays monotonic with respect to actual API usage.
            tc_text = json.dumps(tcs, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            tc_text = str(tcs)
        tool_calls_tokens = _count_text_tokens(tc_text)

    # Streaming (not-yet-snapshotted) case: callers may provide
    # incremental tool-call arguments under a dedicated key.
    elif "tool_calls_delta" in msg:
        tcd = msg.get("tool_calls_delta")
        try:
            tc_text = json.dumps(tcd, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            tc_text = str(tcd)
        tool_calls_tokens = _count_text_tokens(tc_text)

    return _PerMessageTokens(
        index=index,
        role=role,
        content_tokens=content_tokens,
        reasoning_tokens=reasoning_tokens,
        tool_calls_tokens=tool_calls_tokens,
    )


def _model_key_from_message(msg: Dict[str, Any]) -> Optional[str]:
    mk = msg.get("model_key")
    if isinstance(mk, str) and mk.strip():
        return mk.strip()
    return None


def _token_stats_for_messages(
    messages: List[Dict[str, Any]],
    *,
    base_context_tokens: int = 0,
    base_prev_call_input_tokens: Optional[int] = None,
    base_prev_call_model_key: Optional[str] = None,
    base_prev_call_input_tokens_by_model: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    """Compute token stats for a list of messages.

    This is a generalized form of :func:`snapshot_token_stats` used for
    both snapshot and streaming (tail) accounting.

    ``base_context_tokens`` shifts *input token* accounting for assistant
    calls in this list: the input for an assistant call is treated as
    ``base_context_tokens + tokens(before that assistant within this list)``.

    Cached input heuristic
    ----------------------
    We estimate cached input tokens by comparing the current call's input
    length to the most recent prior call *for the same model key*.

    This allows patterns like modelA -> modelB -> modelA to still benefit
    from caching when providers keep per-model KV cache warm.

    ``base_prev_call_input_tokens_by_model`` (and the legacy
    ``base_prev_call_input_tokens``/``base_prev_call_model_key``) allow this
    heuristic to continue across boundaries (e.g. snapshot -> streaming tail).
    """

    msgs = messages or []

    per_message: Dict[str, Any] = {}
    context_tokens = 0
    include_in_context: List[bool] = []

    for idx, m in enumerate(msgs):
        if not isinstance(m, dict):
            include_in_context.append(False)
            continue

        pm = _tokens_for_message(m, idx)

        msg_id = m.get("msg_id")
        if not isinstance(msg_id, str) or not msg_id:
            msg_id = f"idx-{idx}"

        per_message[msg_id] = {
            "index": pm.index,
            "role": pm.role,
            "content_tokens": pm.content_tokens,
            "reasoning_tokens": pm.reasoning_tokens,
            "tool_calls_tokens": pm.tool_calls_tokens,
            "total_tokens": pm.total_tokens,
        }

        role = pm.role
        no_api = bool(m.get("no_api"))
        if (not no_api) and role in ("system", "user", "assistant", "tool"):
            context_tokens += pm.total_tokens
            include_in_context.append(True)
        else:
            include_in_context.append(False)

    # Prefix sum of context tokens within this list.
    prefix_ctx: List[int] = []
    running = 0
    for idx, m in enumerate(msgs):
        if isinstance(m, dict):
            msg_id = m.get("msg_id")
            if not isinstance(msg_id, str) or not msg_id:
                msg_id = f"idx-{idx}"
            if include_in_context[idx]:
                running += int(per_message[msg_id]["total_tokens"])
        prefix_ctx.append(running)

    total_input_tokens = 0
    total_output_tokens = 0
    total_reasoning_tokens = 0  # Subset of output tokens (for display, not cost)
    approx_call_count = 0
    cached_tokens = 0

    cached_input_tokens = 0

    # Track the last observed input token count per model so we can
    # estimate cached tokens even when models switch and later switch
    # back.
    last_input_tokens_by_model: Dict[str, int] = {}
    if isinstance(base_prev_call_input_tokens_by_model, dict):
        for k, v in base_prev_call_input_tokens_by_model.items():
            try:
                if isinstance(k, str) and k and isinstance(v, int) and v >= 0:
                    last_input_tokens_by_model[k] = int(v)
            except Exception:
                continue
    # Backward compatible seed.
    if (
        base_prev_call_model_key
        and isinstance(base_prev_call_model_key, str)
        and base_prev_call_input_tokens is not None
        and isinstance(base_prev_call_input_tokens, int)
        and base_prev_call_input_tokens >= 0
        and base_prev_call_model_key not in last_input_tokens_by_model
    ):
        last_input_tokens_by_model[str(base_prev_call_model_key)] = int(base_prev_call_input_tokens)

    # Per-model usage breakdown (respects model.switch by attributing each
    # assistant message to its model_key, when provided).
    by_model: Dict[str, Dict[str, int]] = {}

    def _bm(mk: Optional[str]) -> Dict[str, int]:
        k = mk or "(unknown)"
        if k not in by_model:
            by_model[k] = {
                "total_input_tokens": 0,
                "cached_input_tokens": 0,
                "total_output_tokens": 0,
                "total_reasoning_tokens": 0,  # Subset of output tokens
                "approx_call_count": 0,
            }
        return by_model[k]

    last_call_input_tokens: Optional[int] = None
    last_call_model_key: Optional[str] = None

    for idx, m in enumerate(msgs):
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "")
        no_api = bool(m.get("no_api"))
        if role != "assistant" or no_api:
            continue

        approx_call_count += 1

        # Input tokens for this call ~= base_context_tokens + context tokens
        # from this list up to the previous message.
        input_tok = int(base_context_tokens) + (prefix_ctx[idx - 1] if idx > 0 else 0)
        total_input_tokens += input_tok

        # Determine the model key for attribution.
        mk = _model_key_from_message(m)

        # Cached-input heuristic: compare against the most recent prior call
        # for the *same model*.
        cached_for_call = 0
        if mk:
            prev_for_model = last_input_tokens_by_model.get(mk)
            if isinstance(prev_for_model, int) and prev_for_model > 0:
                cached_for_call = min(int(prev_for_model), int(input_tok))
                cached_input_tokens += cached_for_call
            last_input_tokens_by_model[mk] = int(input_tok)

        msg_id = m.get("msg_id")
        if not isinstance(msg_id, str) or not msg_id:
            msg_id = f"idx-{idx}"
        msg_stats = per_message.get(msg_id, {})
        out_tok = int(msg_stats.get("total_tokens", 0))
        reason_tok = int(msg_stats.get("reasoning_tokens", 0))
        total_output_tokens += out_tok
        total_reasoning_tokens += reason_tok

        cached_tokens = input_tok
        last_call_input_tokens = input_tok
        last_call_model_key = mk

        bm = _bm(mk)
        bm["approx_call_count"] += 1
        bm["total_input_tokens"] += int(input_tok)
        bm["cached_input_tokens"] += int(cached_for_call)
        bm["total_output_tokens"] += int(out_tok)
        bm["total_reasoning_tokens"] += int(reason_tok)

    api_usage = {
        "total_input_tokens": int(total_input_tokens),
        "total_output_tokens": int(total_output_tokens),
        "total_reasoning_tokens": int(total_reasoning_tokens),  # Subset of output
        "cached_tokens": int(cached_tokens),
        "approx_call_count": int(approx_call_count),
        "cached_input_tokens": int(cached_input_tokens),
        "by_model": by_model,
        # Helper fields to allow streaming_token_stats to chain cache
        # accounting across snapshot->tail boundaries.
        "last_call_input_tokens": int(last_call_input_tokens) if last_call_input_tokens is not None else None,
        "last_call_model_key": last_call_model_key,
        "last_call_input_tokens_by_model": dict(last_input_tokens_by_model),
    }

    return {
        "per_message": per_message,
        "context_tokens": int(context_tokens),
        "api_usage": api_usage,
    }


def snapshot_token_stats(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Compute approximate token statistics for a snapshot.

    This is the same structure as before, but internally it is now
    implemented via :func:`_token_stats_for_messages` so that the same
    logic can be reused for streaming-tail token accounting.
    """

    msgs = snapshot.get("messages") or []
    if not isinstance(msgs, list):
        msgs = []
    return _token_stats_for_messages([m for m in msgs if isinstance(m, dict)])


def streaming_token_stats(db: "ThreadsDB", thread_id: str) -> Dict[str, Any]:
    """Compute token stats for the portion of the thread not in the snapshot.

    This is meant for *live* monitoring.

    It counts:
      * all ``msg.create`` events after the thread's last snapshot, and
      * any currently-streaming ``stream.delta`` events for the thread's
        active invoke.

    The output schema matches :func:`snapshot_token_stats`.

    Notes
    -----
    * This is best-effort and approximate.
    * When a turn is streaming, we synthesize an in-memory assistant
      message from accumulated deltas.
    """

    base_ctx_tokens = 0
    base_prev_call_input_tokens: Optional[int] = None
    base_prev_call_model_key: Optional[str] = None
    base_prev_call_by_model: Optional[Dict[str, int]] = None
    after_seq = -1

    # Read snapshot boundary and (if available) cached token stats.
    try:
        th = db.get_thread(thread_id)
    except Exception:
        th = None

    if th is not None:
        try:
            after_seq = int(getattr(th, "snapshot_last_event_seq", -1) or -1)
        except Exception:
            after_seq = -1

        snap_raw = getattr(th, "snapshot_json", None)
        if isinstance(snap_raw, str) and snap_raw:
            try:
                snap = json.loads(snap_raw)
            except Exception:
                snap = None
            if isinstance(snap, dict):
                ts = snap.get("token_stats")
                if isinstance(ts, dict):
                    try:
                        base_ctx_tokens = int(ts.get("context_tokens") or 0)
                    except Exception:
                        base_ctx_tokens = 0
                    au = ts.get("api_usage")
                    if isinstance(au, dict):
                        lci = au.get("last_call_input_tokens")
                        if isinstance(lci, int) and lci >= 0:
                            base_prev_call_input_tokens = lci
                        lmk = au.get("last_call_model_key")
                        if isinstance(lmk, str) and lmk.strip():
                            base_prev_call_model_key = lmk.strip()
                        lcbm = au.get('last_call_input_tokens_by_model')
                        if isinstance(lcbm, dict):
                            # Coerce values to ints.
                            bm: Dict[str, int] = {}
                            for k, v in lcbm.items():
                                if not isinstance(k, str) or not k:
                                    continue
                                try:
                                    iv = int(v)
                                except Exception:
                                    continue
                                if iv >= 0:
                                    bm[k] = iv
                            base_prev_call_by_model = bm

    # Resolve active invoke (if any).
    open_invoke: Optional[str] = None
    try:
        row_open = db.current_open(thread_id)
        if row_open is not None:
            try:
                open_invoke = row_open["invoke_id"]
            except Exception:
                open_invoke = None
    except Exception:
        open_invoke = None

    # Only treat an invoke as "streaming" if it doesn't already have a close.
    has_close_for_open = False
    if open_invoke:
        try:
            row = db.conn.execute(
                "SELECT 1 FROM events WHERE invoke_id=? AND type='stream.close' LIMIT 1",
                (open_invoke,),
            ).fetchone()
            has_close_for_open = row is not None
        except Exception:
            has_close_for_open = False

    messages: List[Dict[str, Any]] = []

    # Collect msg_ids that have been marked as skipped via msg.edit events.
    # These should not be counted in token stats (they're excluded from API calls).
    skipped_msg_ids: set = set()
    try:
        cur_edit = db.conn.execute(
            "SELECT msg_id, payload_json FROM events WHERE thread_id=? AND type='msg.edit'",
            (thread_id,),
        )
        for edit_msg_id, edit_pj in cur_edit.fetchall():
            try:
                edit_payload = json.loads(edit_pj) if isinstance(edit_pj, str) else (edit_pj or {})
            except Exception:
                edit_payload = {}
            if edit_payload.get('skipped_on_continue'):
                skipped_msg_ids.add(edit_msg_id)
    except Exception:
        pass

    # Streaming accumulators (only for the currently open invoke).
    stream_model_key: Optional[str] = None
    stream_text_parts: List[str] = []
    stream_reason_parts: List[str] = []
    stream_tool_calls: Dict[str, Dict[str, str]] = {}  # tcid -> {name, arguments}
    stream_tool_outputs: Dict[str, List[str]] = {}  # tool_call_id -> [text parts]

    # If we haven't seen a delta with model_key yet, try to read it from stream.open.
    if open_invoke and not has_close_for_open:
        try:
            row = db.conn.execute(
                "SELECT payload_json FROM events WHERE invoke_id=? AND type='stream.open' ORDER BY event_seq DESC LIMIT 1",
                (open_invoke,),
            ).fetchone()
            if row is not None:
                try:
                    pj = row[0]
                    payload = json.loads(pj) if isinstance(pj, str) else (pj or {})
                except Exception:
                    payload = {}
                mk = (payload or {}).get("model_key")
                if isinstance(mk, str) and mk.strip():
                    stream_model_key = mk.strip()
        except Exception:
            pass

    # Scan events after snapshot.
    try:
        cur = db.conn.execute(
            "SELECT event_seq, type, msg_id, invoke_id, ts, payload_json "
            "FROM events WHERE thread_id=? AND event_seq>? ORDER BY event_seq ASC",
            (thread_id, after_seq),
        )
        rows = cur.fetchall()
    except Exception:
        rows = []

    for _ev_seq, ev_type, msg_id, inv, ts, pj in rows:
        try:
            payload = json.loads(pj) if isinstance(pj, str) else (pj or {})
        except Exception:
            payload = {}

        if ev_type == "msg.create":
            # Skip messages that have been marked as skipped_on_continue
            if msg_id and msg_id in skipped_msg_ids:
                continue
            m = dict(payload) if isinstance(payload, dict) else {}
            m["msg_id"] = msg_id
            m["ts"] = ts
            if "role" not in m and isinstance(payload, dict):
                m["role"] = payload.get("role")
            messages.append(m)
            continue

        if (
            open_invoke
            and not has_close_for_open
            and ev_type == "stream.delta"
            and isinstance(inv, str)
            and inv == open_invoke
        ):
            if not isinstance(payload, dict):
                continue

            mk = payload.get("model_key")
            if isinstance(mk, str) and mk.strip():
                stream_model_key = mk.strip()

            txt = payload.get("text")
            if isinstance(txt, str) and txt:
                stream_text_parts.append(txt)

            rs = payload.get("reason")
            if isinstance(rs, str) and rs:
                stream_reason_parts.append(rs)

            tc = payload.get("tool_call")
            if isinstance(tc, dict):
                tcid = str(tc.get("id") or "")
                name = str(tc.get("name") or "")
                args_delta = tc.get("arguments_delta")
                if tcid and isinstance(args_delta, str) and args_delta:
                    entry = stream_tool_calls.get(tcid) or {"name": name, "arguments": ""}
                    if name and not entry.get("name"):
                        entry["name"] = name
                    entry["arguments"] = (entry.get("arguments") or "") + args_delta
                    stream_tool_calls[tcid] = entry

            tl = payload.get("tool")
            if isinstance(tl, dict):
                tcid = str(tl.get("id") or "")
                text = tl.get("text")
                if tcid and isinstance(text, str) and text:
                    stream_tool_outputs.setdefault(tcid, []).append(text)

    # Convert streaming accumulators to synthetic messages.
    if open_invoke and not has_close_for_open:
        tool_calls_delta: List[Dict[str, Any]] = []
        for tcid, info in stream_tool_calls.items():
            tool_calls_delta.append(
                {
                    "id": tcid,
                    "function": {
                        "name": info.get("name") or "",
                        "arguments": info.get("arguments") or "",
                    },
                }
            )

        if stream_text_parts or stream_reason_parts or tool_calls_delta:
            m: Dict[str, Any] = {
                "msg_id": f"stream:{open_invoke}:assistant",
                "role": "assistant",
                "content": "".join(stream_text_parts),
                "reasoning": "".join(stream_reason_parts),
            }
            if stream_model_key:
                m["model_key"] = stream_model_key
            if tool_calls_delta:
                m["tool_calls_delta"] = tool_calls_delta
            messages.append(m)

        for tcid, parts in stream_tool_outputs.items():
            messages.append(
                {
                    "msg_id": f"stream:{open_invoke}:tool:{tcid}",
                    "role": "tool",
                    "tool_call_id": tcid,
                    "content": "".join(parts),
                }
            )

    return _token_stats_for_messages(
        [m for m in messages if isinstance(m, dict)],
        base_context_tokens=base_ctx_tokens,
        base_prev_call_input_tokens=base_prev_call_input_tokens,
        base_prev_call_model_key=base_prev_call_model_key,
        base_prev_call_input_tokens_by_model=base_prev_call_by_model,
    )


def _merge_token_stats(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    """Merge two token_stats dicts (snapshot + streaming tail)."""

    def _int(x: Any) -> int:
        try:
            return int(x or 0)
        except Exception:
            return 0

    out: Dict[str, Any] = {
        "per_message": {},
        "context_tokens": _int(a.get("context_tokens")) + _int(b.get("context_tokens")),
        "api_usage": {},
    }

    pm: Dict[str, Any] = {}
    for src in (a.get("per_message") or {}, b.get("per_message") or {}):
        if isinstance(src, dict):
            pm.update(src)
    out["per_message"] = pm

    au_a = a.get("api_usage") if isinstance(a.get("api_usage"), dict) else {}
    au_b = b.get("api_usage") if isinstance(b.get("api_usage"), dict) else {}

    total_input = _int(au_a.get("total_input_tokens")) + _int(au_b.get("total_input_tokens"))
    total_output = _int(au_a.get("total_output_tokens")) + _int(au_b.get("total_output_tokens"))
    total_reasoning = _int(au_a.get("total_reasoning_tokens")) + _int(au_b.get("total_reasoning_tokens"))
    cached_in = _int(au_a.get("cached_input_tokens")) + _int(au_b.get("cached_input_tokens"))
    call_count = _int(au_a.get("approx_call_count")) + _int(au_b.get("approx_call_count"))

    # cached_tokens should reflect the most recent call.
    cached_tokens = au_b.get("cached_tokens") if _int(au_b.get("approx_call_count")) > 0 else au_a.get("cached_tokens")

    last_call_input_tokens = (
        au_b.get("last_call_input_tokens") if _int(au_b.get("approx_call_count")) > 0 else au_a.get("last_call_input_tokens")
    )
    last_call_model_key = (
        au_b.get("last_call_model_key") if _int(au_b.get("approx_call_count")) > 0 else au_a.get("last_call_model_key")
    )

    # Merge last_call_input_tokens_by_model (best-effort): prefer b's
    # mapping when present, otherwise a's.
    lcbm = None
    if isinstance(au_b.get('last_call_input_tokens_by_model'), dict) and _int(au_b.get('approx_call_count')) > 0:
        lcbm = au_b.get('last_call_input_tokens_by_model')
    elif isinstance(au_a.get('last_call_input_tokens_by_model'), dict):
        lcbm = au_a.get('last_call_input_tokens_by_model')

    # Merge by_model usage.
    by_model: Dict[str, Dict[str, int]] = {}
    for src in (au_a.get("by_model"), au_b.get("by_model")):
        if not isinstance(src, dict):
            continue
        for mk, usage in src.items():
            if not isinstance(mk, str) or not isinstance(usage, dict):
                continue
            bm = by_model.setdefault(
                mk,
                {"total_input_tokens": 0, "cached_input_tokens": 0, "total_output_tokens": 0, "total_reasoning_tokens": 0, "approx_call_count": 0},
            )
            bm["total_input_tokens"] += _int(usage.get("total_input_tokens"))
            bm["cached_input_tokens"] += _int(usage.get("cached_input_tokens"))
            bm["total_output_tokens"] += _int(usage.get("total_output_tokens"))
            bm["total_reasoning_tokens"] += _int(usage.get("total_reasoning_tokens"))
            bm["approx_call_count"] += _int(usage.get("approx_call_count"))

    out["api_usage"] = {
        "total_input_tokens": int(total_input),
        "total_output_tokens": int(total_output),
        "total_reasoning_tokens": int(total_reasoning),  # Subset of output tokens
        "cached_tokens": int(_int(cached_tokens)),
        "approx_call_count": int(call_count),
        "cached_input_tokens": int(cached_in),
        "by_model": by_model,
        "last_call_input_tokens": last_call_input_tokens,
        "last_call_model_key": last_call_model_key,
        "last_call_input_tokens_by_model": lcbm if isinstance(lcbm, dict) else {},
    }

    return out


def _cost_for_usage(usage: Dict[str, Any], *, model_key: str, llm: Any) -> Dict[str, float]:
    """Compute cost (USD) for a usage dict using an eggllm-like client.

    We rely only on the public eggllm API:
      llm.current_model_cost_config(model_key) ->
        {input_tokens, cached_input, output_tokens} in USD per 1K tokens.
    """

    if llm is None:
        return {"input": 0.0, "cached": 0.0, "output": 0.0, "total": 0.0}

    # Resolve aliases / provider-prefix keys to a canonical registry key
    # when possible (eggllm supports e.g. "baseten:Openai-120b").
    resolved_key = model_key
    try:
        reg = getattr(llm, 'registry', None)
        if reg is not None and hasattr(reg, 'resolve'):
            rk = reg.resolve(model_key)  # type: ignore[attr-defined]
            if isinstance(rk, str) and rk:
                resolved_key = rk
    except Exception:
        resolved_key = model_key

    try:
        cfg = llm.current_model_cost_config(resolved_key)  # type: ignore[attr-defined]
    except Exception:
        cfg = {}

    try:
        pin = float((cfg or {}).get("input_tokens") or 0.0)
        pcached = float((cfg or {}).get("cached_input") or 0.0)
        pout = float((cfg or {}).get("output_tokens") or 0.0)
    except Exception:
        pin = pcached = pout = 0.0

    def _usd(tokens: int, price_per_1k: float) -> float:
        if tokens <= 0 or price_per_1k <= 0.0:
            return 0.0
        return float(tokens) * (price_per_1k / 1000.0)

    try:
        cin_total = int(usage.get("total_input_tokens") or 0)
        ccached = int(usage.get("cached_input_tokens") or 0)
        cout = int(usage.get("total_output_tokens") or 0)
    except Exception:
        cin_total = ccached = cout = 0

    new_in = max(cin_total - ccached, 0)
    c_in = _usd(new_in, pin)
    c_cached = _usd(ccached, pcached)
    c_out = _usd(cout, pout)
    return {"input": c_in, "cached": c_cached, "output": c_out, "total": c_in + c_cached + c_out}


def _attach_costs(stats: Dict[str, Any], *, llm: Any = None) -> Dict[str, Any]:
    """Attach cost estimates (USD) under api_usage.cost_usd.

    Cost is derived from the per-model usage breakdown (api_usage.by_model).
    When no cost config is available, totals are zero.
    """

    au = stats.get("api_usage") if isinstance(stats.get("api_usage"), dict) else {}
    by_model = au.get("by_model") if isinstance(au.get("by_model"), dict) else {}

    costs_by_model: Dict[str, Dict[str, float]] = {}
    total = 0.0

    warnings: List[str] = []

    for mk, usage in by_model.items():
        if not isinstance(mk, str) or not isinstance(usage, dict):
            continue
        if mk == "(unknown)":
            continue
        c = _cost_for_usage(usage, model_key=mk, llm=llm)
        if float(c.get('total') or 0.0) <= 0.0:
            # Heuristic: most likely no cost config.
            try:
                # Best-effort: resolve aliases/provider-prefix keys.
                resolved_key = mk
                try:
                    reg = getattr(llm, 'registry', None)
                    if reg is not None and hasattr(reg, 'resolve'):
                        rk = reg.resolve(mk)  # type: ignore[attr-defined]
                        if isinstance(rk, str) and rk:
                            resolved_key = rk
                except Exception:
                    resolved_key = mk

                cfg = llm.current_model_cost_config(resolved_key)  # type: ignore[attr-defined]
                has_any = bool(
                    float((cfg or {}).get('input_tokens') or 0.0)
                    or float((cfg or {}).get('cached_input') or 0.0)
                    or float((cfg or {}).get('output_tokens') or 0.0)
                )
                if not has_any:
                    warnings.append(f"No cost config for model: {mk}")
            except Exception:
                warnings.append(f"No cost config for model: {mk}")
        costs_by_model[mk] = c
        total += float(c.get("total") or 0.0)

    au2 = dict(au)
    au2["cost_usd"] = {
        "by_model": costs_by_model,
        "total": float(total),
        "warnings": warnings,
    }

    out = dict(stats)
    out["api_usage"] = au2
    return out


def _get_skipped_msg_ids(db: "ThreadsDB", thread_id: str) -> set:
    """Query msg.edit events to find messages marked as skipped_on_continue.

    This is the source of truth for which messages are excluded from API calls.
    Used to filter snapshot messages when the snapshot might be stale.
    """
    skipped: set = set()
    try:
        cur = db.conn.execute(
            "SELECT msg_id, payload_json FROM events WHERE thread_id=? AND type='msg.edit'",
            (thread_id,),
        )
        for msg_id, pj in cur.fetchall():
            try:
                payload = json.loads(pj) if isinstance(pj, str) else (pj or {})
            except Exception:
                payload = {}
            if payload.get('skipped_on_continue'):
                skipped.add(msg_id)
    except Exception:
        pass
    return skipped


def total_token_stats(db: "ThreadsDB", thread_id: str, *, llm: Any = None) -> Dict[str, Any]:
    """Return snapshot+streaming token stats (optionally including cost).

    This is the recommended helper for UIs that want a single structure
    describing the thread's approximate context length and token usage.

    If ``llm`` is provided (e.g. an ``eggllm.LLMClient`` instance), we also
    attach an approximate USD cost estimate under ``api_usage.cost_usd``.

    The merged result is conceptually:

      total ~= snapshot_token_stats + streaming_token_stats

    (with careful handling of cached_tokens / last-call metadata).
    """

    # Snapshot token_stats (if present) are preferred because they are cached.
    snap_stats: Dict[str, Any] = {"per_message": {}, "context_tokens": 0, "api_usage": {}}
    try:
        th = db.get_thread(thread_id)
    except Exception:
        th = None

    if th is not None:
        snap_raw = getattr(th, "snapshot_json", None)
        if isinstance(snap_raw, str) and snap_raw:
            try:
                snap = json.loads(snap_raw)
            except Exception:
                snap = None
            if isinstance(snap, dict):
                # Query current skipped msg_ids from msg.edit events.
                # This is the source of truth for which messages are excluded.
                skipped_msg_ids = _get_skipped_msg_ids(db, thread_id)

                # Check if any skipped messages are in the snapshot's message list.
                # If so, the cached token_stats is stale and we must recalculate.
                msgs = snap.get("messages") or []
                snap_msg_ids = {m.get("msg_id") for m in msgs if isinstance(m, dict)}
                has_stale_skipped = bool(skipped_msg_ids & snap_msg_ids)

                ts = snap.get("token_stats")
                if isinstance(ts, dict) and not has_stale_skipped:
                    # Use cached token_stats (for performance) when accurate.
                    snap_stats = ts
                else:
                    # Recalculate with filtered messages to exclude skipped ones.
                    filtered_msgs = [
                        m for m in msgs
                        if isinstance(m, dict) and m.get("msg_id") not in skipped_msg_ids
                    ]
                    snap_stats = _token_stats_for_messages(filtered_msgs)

    stream_stats = streaming_token_stats(db, thread_id)
    total = _merge_token_stats(snap_stats, stream_stats)
    if llm is not None:
        total = _attach_costs(total, llm=llm)
    return total


def _example_cost_cfg_note() -> str:
    return (
        "Cost estimates require per-model cost config in models.json. "
        "Add a 'cost' block (cents per 1K tokens) to the model entry, e.g. "
        "{\"cost\": {\"input_tokens\": 0.25, \"cached_input\": 0.05, \"output_tokens\": 1.00}}."
    )


__all__ = [
    "count_text_tokens",
    "llm_message_tps_for_invoke",
    "live_llm_tps_for_invoke",
    "tool_message_tps_for_call",
    "snapshot_token_stats",
    "streaming_token_stats",
    "total_token_stats",
]

