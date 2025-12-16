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
from typing import Any, Dict, List, Optional, Tuple


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
    tcs = msg.get("tool_calls") or []
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

    return _PerMessageTokens(
        index=index,
        role=role,
        content_tokens=content_tokens,
        reasoning_tokens=reasoning_tokens,
        tool_calls_tokens=tool_calls_tokens,
    )


def snapshot_token_stats(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Compute approximate token statistics for a snapshot.

    The ``snapshot`` object is expected to be a dict with a
    ``"messages"`` key, as produced by :class:`SnapshotBuilder`.  The
    returned structure is safe to store back inside the snapshot under
    the key ``"token_stats"``.
    """

    msgs = snapshot.get("messages") or []
    if not isinstance(msgs, list):
        msgs = []

    per_message: Dict[str, Any] = {}

    # First pass: per‑message token counts and approximate context size.
    context_tokens = 0

    # Track which messages contribute to context for API calls.  We
    # include messages that:
    #   * have a standard OpenAI role (system/user/assistant/tool)
    #   * are *not* marked no_api=True
    #
    # This mirrors the high‑level intent of ThreadRunner.
    include_in_context: List[bool] = []

    for idx, m in enumerate(msgs):
        if not isinstance(m, dict):
            include_in_context.append(False)
            continue

        pm = _tokens_for_message(m, idx)

        msg_id = m.get("msg_id")
        if not isinstance(msg_id, str) or not msg_id:
            # Fallback – msg_id should normally be present for
            # msg.create events, but we guard defensively.
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

    # Second pass: approximate API usage by inferring LLM calls.
    #
    # Heuristic: each assistant message is treated as the result of an
    # LLM call whose *input* was the full context (all earlier messages
    # that we included in context).  This is not perfect – for example
    # we do not currently re‑enact the exact sanitisation logic used in
    # ThreadRunner – but it gives a useful, monotonic lower bound on
    # input and output token usage.

    # Prefix sum of context tokens by message index for quick lookup.
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
    # Context size (in tokens) immediately before the *last* assistant
    # call.  This is useful for UI display but not directly used for
    # cost calculations.
    cached_tokens = 0

    # Heuristic cached input: an estimate of how many input tokens were
    # served from the provider's KV cache across calls. For each
    # assistant call j>1 with context size C_j and previous call context
    # C_{j-1}, we approximate the cached portion as
    #   cached_j ~= min(C_{j-1}, C_j)
    # and sum cached_j over all calls. The remaining
    #   new_j ~= max(0, C_j - C_{j-1})
    # can then be treated as full-price input tokens.
    cached_input_tokens = 0
    last_ctx_for_call: Optional[int] = None

    for idx, m in enumerate(msgs):
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "")
        no_api = bool(m.get("no_api"))
        if role != "assistant" or no_api:
            continue

        approx_call_count += 1
        # Input tokens for this call ~= context tokens up to the
        # *previous* message. Denote this C_j.
        input_tok = prefix_ctx[idx - 1] if idx > 0 else 0
        total_input_tokens += input_tok

        # Cached input heuristic: for calls after the first, treat the
        # shared prefix with the previous call as coming from cache.
        # new_j ~= max(0, C_j - C_{j-1})
        # cached_j ~= min(C_{j-1}, C_j)
        if last_ctx_for_call is not None:
            cached_input_tokens += min(last_ctx_for_call, input_tok)
        last_ctx_for_call = input_tok

        msg_id = m.get("msg_id")
        if not isinstance(msg_id, str) or not msg_id:
            msg_id = f"idx-{idx}"
        out_tok = int(per_message.get(msg_id, {}).get("total_tokens", 0))
        total_output_tokens += out_tok
        # Track the context size before the last assistant call for
        # informational purposes (e.g. UI display).
        cached_tokens = input_tok

    api_usage = {
        "total_input_tokens": int(total_input_tokens),
        "total_output_tokens": int(total_output_tokens),
        "cached_tokens": int(cached_tokens),
        "approx_call_count": int(approx_call_count),
        "cached_input_tokens": int(cached_input_tokens),
    }

    return {
        "per_message": per_message,
        "context_tokens": int(context_tokens),
        "api_usage": api_usage,
    }


__all__ = ["snapshot_token_stats"]
