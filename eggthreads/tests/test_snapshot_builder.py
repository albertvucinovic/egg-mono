"""Tests for :mod:`eggthreads.snapshot`.

These are intentionally fairly high level: we feed a sequence of
``msg.create`` events into :class:`SnapshotBuilder` and assert that the
resulting ``snapshot['messages']`` preserves important flags.

The flags under test (``no_api`` and ``keep_user_turn``) are used by
higher-level runners to decide which user messages should be visible to
the LLM and whether a turn should immediately trigger an assistant
call.  Regressions here are subtle but high impact, so we pin the
behaviour with dedicated tests.
"""

from __future__ import annotations

import json

from eggthreads import SnapshotBuilder


def _msg_create(event_seq: int, msg_id: str, payload: dict) -> dict:
    """Helper to build a minimal ``msg.create`` event dict.

    ``SnapshotBuilder.build`` only looks at a small subset of columns
    (``type``, ``msg_id``, ``event_seq``, ``payload_json``), so the
    helper keeps fixture data concise and focused.
    """

    return {
        "type": "msg.create",
        "msg_id": msg_id,
        "event_seq": event_seq,
        "payload_json": json.dumps(payload),
    }


def test_snapshot_preserves_no_api_and_keep_user_turn_for_user_messages() -> None:
    """``no_api`` and ``keep_user_turn`` flags must survive snapshots.

    The `$` / `$$` command handling in the front-end app relies on
    these flags when reconstructing LLM context.  If they were dropped
    during snapshot building, hidden user commands could leak into
    provider API calls or turns that should keep control with the user
    could accidentally trigger assistant calls.
    """

    builder = SnapshotBuilder()

    events = [
        _msg_create(
            1,
            "msg_hidden",
            {
                "role": "user",
                "content": "$$ echo hidden",
                "no_api": True,
                "keep_user_turn": True,
            },
        ),
        _msg_create(
            2,
            "msg_visible",
            {
                "role": "user",
                "content": "$ echo visible",
                "keep_user_turn": True,
            },
        ),
    ]

    snapshot = builder.build(events)
    messages = snapshot.get("messages", [])

    assert len(messages) == 2

    first, second = messages

    # First message ($$ command) should keep both flags.
    assert first["role"] == "user"
    assert first["content"] == "$$ echo hidden"
    assert first.get("no_api") is True
    assert first.get("keep_user_turn") is True

    # Second message ($ command) should *not* have no_api, but should
    # keep keep_user_turn so the runner knows this turn does not
    # trigger an immediate LLM call.
    assert second["role"] == "user"
    assert second["content"] == "$ echo visible"
    assert "no_api" not in second or second.get("no_api") in (None, False)
    assert second.get("keep_user_turn") is True
