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
import json
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
) -> Dict[str, Any]:
    """Compute token stats for a list of messages.

    This is a generalized form of :func:`snapshot_token_stats` used for
    both snapshot and streaming (tail) accounting.

    ``base_context_tokens`` shifts *input token* accounting for assistant
    calls in this list: the input for an assistant call is treated as
    ``base_context_tokens + tokens(before that assistant within this list)``.

    ``base_prev_call_input_tokens`` and ``base_prev_call_model_key`` allow
    the cached-input heuristic to continue across boundaries (e.g. from
    snapshot -> streaming tail). Cached-input is only attributed when the
    model key stays the same.
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
    approx_call_count = 0
    cached_tokens = 0

    cached_input_tokens = 0
    prev_call_input_tokens: Optional[int] = base_prev_call_input_tokens
    prev_call_model: Optional[str] = base_prev_call_model_key

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

        # Cached-input heuristic only applies if we stay on the same model.
        cached_for_call = 0
        if (
            prev_call_input_tokens is not None
            and prev_call_input_tokens > 0
            and prev_call_model
            and mk
            and prev_call_model == mk
        ):
            cached_for_call = min(int(prev_call_input_tokens), int(input_tok))
            cached_input_tokens += cached_for_call

        prev_call_input_tokens = input_tok
        prev_call_model = mk

        msg_id = m.get("msg_id")
        if not isinstance(msg_id, str) or not msg_id:
            msg_id = f"idx-{idx}"
        out_tok = int(per_message.get(msg_id, {}).get("total_tokens", 0))
        total_output_tokens += out_tok

        cached_tokens = input_tok
        last_call_input_tokens = input_tok
        last_call_model_key = mk

        bm = _bm(mk)
        bm["approx_call_count"] += 1
        bm["total_input_tokens"] += int(input_tok)
        bm["cached_input_tokens"] += int(cached_for_call)
        bm["total_output_tokens"] += int(out_tok)

    api_usage = {
        "total_input_tokens": int(total_input_tokens),
        "total_output_tokens": int(total_output_tokens),
        "cached_tokens": int(cached_tokens),
        "approx_call_count": int(approx_call_count),
        "cached_input_tokens": int(cached_input_tokens),
        "by_model": by_model,
        # Helper fields to allow streaming_token_stats to chain cache
        # accounting across snapshot->tail boundaries.
        "last_call_input_tokens": int(last_call_input_tokens) if last_call_input_tokens is not None else None,
        "last_call_model_key": last_call_model_key,
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
                {"total_input_tokens": 0, "cached_input_tokens": 0, "total_output_tokens": 0, "approx_call_count": 0},
            )
            bm["total_input_tokens"] += _int(usage.get("total_input_tokens"))
            bm["cached_input_tokens"] += _int(usage.get("cached_input_tokens"))
            bm["total_output_tokens"] += _int(usage.get("total_output_tokens"))
            bm["approx_call_count"] += _int(usage.get("approx_call_count"))

    out["api_usage"] = {
        "total_input_tokens": int(total_input),
        "total_output_tokens": int(total_output),
        "cached_tokens": int(_int(cached_tokens)),
        "approx_call_count": int(call_count),
        "cached_input_tokens": int(cached_in),
        "by_model": by_model,
        "last_call_input_tokens": last_call_input_tokens,
        "last_call_model_key": last_call_model_key,
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

    try:
        cfg = llm.current_model_cost_config(model_key)  # type: ignore[attr-defined]
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
                cfg = llm.current_model_cost_config(mk)  # type: ignore[attr-defined]
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
                ts = snap.get("token_stats")
                if isinstance(ts, dict):
                    snap_stats = ts
                else:
                    snap_stats = snapshot_token_stats(snap)

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
    "snapshot_token_stats",
    "streaming_token_stats",
    "total_token_stats",
]

