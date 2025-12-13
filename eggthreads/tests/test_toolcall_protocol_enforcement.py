from __future__ import annotations

"""Tests for assistant/tool_calls protocol enforcement.

These tests exercise the internal helper
``ThreadRunner._enforce_assistant_toolcall_protocol`` with a few
hand‑crafted message sequences so that we can be confident malformed
assistant+tool patterns are cleaned up before talking to the provider.

Behaviour under test:

* Well‑formed assistant(tool_calls) + contiguous tool messages are
  preserved exactly.
* If a user (or other non‑tool) message appears *between* an
  assistant(tool_calls) and its tool messages, the assistant+tool turn
  is dropped from the provider view and the user message keeps
  priority.
* Orphan tool messages whose ``tool_call_id`` does not belong to a
  valid assistant(tool_calls) turn are dropped from the provider view.

The underlying event log is not involved here; we test the pure
in‑memory transformation that runs inside ``_sanitize_messages_for_api``.
"""

from typing import Any, Dict, List

from eggthreads import ThreadRunner  # type: ignore


class _DummyRunner(ThreadRunner):  # type: ignore[misc]
    """Minimal subclass exposing the enforcement helper.

    We purposely do *not* call the real ThreadRunner.__init__; the
    helper we test does not depend on instance attributes, it only
    inspects the provided ``messages`` list.
    """

    def __init__(self) -> None:  # pragma: no cover - trivial
        pass


def _enforce(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    r = _DummyRunner()
    # mypy: the helper is defined on ThreadRunner, but it's an internal
    # method so we call it dynamically.
    return r._enforce_assistant_toolcall_protocol(messages)  # type: ignore[attr-defined]


def test_well_formed_assistant_tool_turn_preserved() -> None:
    msgs = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "I will call a tool.",
            "tool_calls": [
                {"id": "tc1", "function": {"name": "bash", "arguments": "..."}},
            ],
        },
        {"role": "tool", "tool_call_id": "tc1", "content": "ok"},
        {"role": "user", "content": "after"},
    ]

    out = _enforce(msgs)

    assert out == msgs


def test_user_between_assistant_and_tool_drops_tool_turn_keeps_user() -> None:
    msgs = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "I will call a tool.",
            "tool_calls": [
                {"id": "tc1", "function": {"name": "bash", "arguments": "..."}},
            ],
        },
        {"role": "user", "content": "oops, typed while waiting"},
        {"role": "tool", "tool_call_id": "tc1", "content": "ok"},
        {"role": "user", "content": "after"},
    ]

    out = _enforce(msgs)

    # Assistant(tool_calls) + tool are dropped, user messages remain and
    # keep their relative order.
    assert out == [
        {"role": "user", "content": "hi"},
        {"role": "user", "content": "oops, typed while waiting"},
        {"role": "user", "content": "after"},
    ]


def test_mismatched_tool_ids_drop_malformed_turn() -> None:
    msgs = [
        {"role": "user", "content": "start"},
        {
            "role": "assistant",
            "content": "Calling two tools.",
            "tool_calls": [
                {"id": "tc1", "function": {"name": "bash", "arguments": "..."}},
                {"id": "tc2", "function": {"name": "bash", "arguments": "..."}},
            ],
        },
        # Only one tool message, and for the wrong id
        {"role": "tool", "tool_call_id": "tcX", "content": "unexpected"},
        {"role": "user", "content": "end"},
    ]

    out = _enforce(msgs)

    # The malformed assistant+tool segment is removed; surrounding user
    # messages remain.
    assert out == [
        {"role": "user", "content": "start"},
        {"role": "user", "content": "end"},
    ]


def test_orphan_tool_is_dropped() -> None:
    msgs = [
        {"role": "user", "content": "before"},
        {"role": "tool", "tool_call_id": "tc-orphan", "content": "orphan"},
        {"role": "user", "content": "after"},
    ]

    out = _enforce(msgs)

    assert out == [
        {"role": "user", "content": "before"},
        {"role": "user", "content": "after"},
    ]
