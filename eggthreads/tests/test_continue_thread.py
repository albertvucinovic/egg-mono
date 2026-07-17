"""Tests for continue_thread recovery functionality.

These tests verify that crash recovery works correctly:
1. Threads with expired leases can be continued
2. RA1 boundary is reset after continue_thread
3. Skipped messages don't block RA1
4. list_active_threads respects lease expiration
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from eggthreads import (
    ThreadsDB,
    create_root_thread,
    create_child_thread,
    append_message,
    create_snapshot,
    diagnose_thread,
    continue_thread,
    continue_thread_manually,
    is_thread_continuable,
    wait_thread_settled,
    validate_continue_target,
)
from eggthreads.api import append_continue_recovery_notice, append_recovery_notice, interrupt_thread, list_active_threads, collect_subtree, get_thread_recovery, set_thread_recovery
from eggthreads.tool_state import discover_runner_actionable, _last_stream_close_seq


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _make_temp_db(tmp_path) -> tuple[ThreadsDB, Path]:
    """Create a ThreadsDB bound to a temporary SQLite file."""
    db_path = tmp_path / "threads.sqlite"
    db = ThreadsDB(db_path)
    db.init_schema()
    return db, db_path


def _ulid_like() -> str:
    """Generate a unique ID for testing."""
    import uuid
    return uuid.uuid4().hex[:26].upper()


class TestLeaseExpiration:
    """Tests for lease expiration handling in recovery."""

    def test_continue_thread_proceeds_with_expired_lease(self, tmp_path):
        """continue_thread should proceed if lease is expired."""
        db, _ = _make_temp_db(tmp_path)
        tid = create_root_thread(db, name="test")
        append_message(db, tid, "user", "Hello")

        # Simulate a stale lease (expired 1 minute ago)
        expired_time = (_utcnow() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
        db.conn.execute(
            "INSERT INTO open_streams (thread_id, invoke_id, lease_until, owner, purpose) VALUES (?, ?, ?, ?, ?)",
            (tid, "stale-invoke", expired_time, "crashed-process", "llm"),
        )

        # continue_thread should succeed despite the stale lease
        result = continue_thread(db, tid)
        assert result.success is True
        assert "healthy" in result.message.lower() or result.success

    def test_continue_thread_blocks_with_active_lease(self, tmp_path):
        """continue_thread should fail if lease is still active."""
        db, _ = _make_temp_db(tmp_path)
        tid = create_root_thread(db, name="test")
        append_message(db, tid, "user", "Hello")

        # Simulate an active lease (expires in 1 minute)
        active_time = (_utcnow() + timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
        db.conn.execute(
            "INSERT INTO open_streams (thread_id, invoke_id, lease_until, owner, purpose) VALUES (?, ?, ?, ?, ?)",
            (tid, "active-invoke", active_time, "running-process", "llm"),
        )

        # continue_thread should fail
        result = continue_thread(db, tid)
        assert result.success is False
        assert "running" in result.message.lower()

    def test_is_thread_continuable_with_expired_lease(self, tmp_path):
        """is_thread_continuable should return True with expired lease."""
        db, _ = _make_temp_db(tmp_path)
        tid = create_root_thread(db, name="test")
        append_message(db, tid, "user", "Hello")

        # Add expired lease
        expired_time = (_utcnow() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
        db.conn.execute(
            "INSERT INTO open_streams (thread_id, invoke_id, lease_until, owner, purpose) VALUES (?, ?, ?, ?, ?)",
            (tid, "stale-invoke", expired_time, "crashed-process", "llm"),
        )

        assert is_thread_continuable(db, tid) is True

    def test_list_active_threads_ignores_expired_lease(self, tmp_path):
        """list_active_threads should not count threads with expired leases as running."""
        db, _ = _make_temp_db(tmp_path)
        tid = create_root_thread(db, name="test")

        # Add expired lease
        expired_time = (_utcnow() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
        db.conn.execute(
            "INSERT INTO open_streams (thread_id, invoke_id, lease_until, owner, purpose) VALUES (?, ?, ?, ?, ?)",
            (tid, "stale-invoke", expired_time, "crashed-process", "llm"),
        )

        subtree = collect_subtree(db, tid)
        active = list_active_threads(db, subtree)

        # Thread should not be in active list (expired lease, no runnable work)
        assert tid not in active


class TestRA1BoundaryReset:
    """Tests for RA1 boundary reset after continue_thread."""

    def test_continue_thread_resets_ra1_boundary(self, tmp_path):
        """continue_thread with explicit msg_id should reset RA1 boundary."""
        db, _ = _make_temp_db(tmp_path)
        tid = create_root_thread(db, name="test")

        # Add system and user messages
        append_message(db, tid, "system", "You are helpful")
        append_message(db, tid, "user", "Hello")

        # Simulate an interrupt that advanced the RA1 boundary past the user message
        user_msg_seq = db.conn.execute(
            "SELECT event_seq FROM events WHERE thread_id=? AND type='msg.create' ORDER BY event_seq DESC LIMIT 1",
            (tid,),
        ).fetchone()[0]

        # Add control.interrupt with purpose='llm' (advances boundary)
        db.append_event(
            event_id=_ulid_like(),
            thread_id=tid,
            type_='control.interrupt',
            payload={'reason': 'test', 'purpose': 'llm'},
        )

        # RA1 should NOT trigger (boundary is past user message)
        ra = discover_runner_actionable(db, tid)
        assert ra is None, "RA1 should not trigger after interrupt with purpose='llm'"

        # Get the user message ID
        user_msg_id = db.conn.execute(
            "SELECT msg_id FROM events WHERE thread_id=? AND type='msg.create' AND payload_json LIKE '%user%' ORDER BY event_seq DESC LIMIT 1",
            (tid,),
        ).fetchone()[0]

        # Call continue_thread with explicit user message
        result = continue_thread(db, tid, user_msg_id)
        assert result.success is True

        # Now RA1 should trigger
        ra = discover_runner_actionable(db, tid)
        assert ra is not None, "RA1 should trigger after continue_thread resets boundary"
        assert ra.kind == "RA1_llm"

    def test_continue_thread_repairs_cancelled_pending_ra1(self, tmp_path):
        """Auto-detect /continue should recover a pending RA1 cancelled before lease acquisition."""
        db, _ = _make_temp_db(tmp_path)
        tid = create_root_thread(db, name="test")

        user_msg_id = append_message(db, tid, "user", "Hello")

        assert discover_runner_actionable(db, tid) is not None

        # Simulate Ctrl+C-style cancellation while the user message is only
        # pending (no open_stream lease yet).  This advances the RA1 boundary
        # past the user message without creating an assistant/system result.
        interrupt_thread(db, tid, reason="continue")
        assert discover_runner_actionable(db, tid) is None

        diagnosis = diagnose_thread(db, tid)
        assert diagnosis.is_healthy is False
        assert diagnosis.suggested_continue_point == user_msg_id
        assert diagnosis.details["stuck_ra1_trigger_msg_id"] == user_msg_id

        result = continue_thread(db, tid)
        assert result.success is True
        assert result.continue_from_msg_id == user_msg_id

        ra = discover_runner_actionable(db, tid)
        assert ra is not None
        assert ra.kind == "RA1_llm"
        assert ra.msg_id == user_msg_id

    def test_manual_continue_retries_user_after_auto_continue_stopped_notice(self, tmp_path):
        """No-arg manual /continue should retry the trigger, not the local notice."""
        db, _ = _make_temp_db(tmp_path)
        tid = create_root_thread(db, name="test")

        user_msg_id = append_message(db, tid, "user", "Hello")
        invoke_id = _ulid_like()
        db.append_event(
            event_id=_ulid_like(),
            thread_id=tid,
            type_="stream.open",
            payload={"stream_kind": "llm"},
            msg_id=_ulid_like(),
            invoke_id=invoke_id,
        )
        db.append_event(
            event_id=_ulid_like(),
            thread_id=tid,
            type_="stream.delta",
            payload={"reason": "LLM/runner error: HTTP 400 Bad Request"},
            invoke_id=invoke_id,
            chunk_seq=0,
        )
        error_msg_id = append_message(
            db,
            tid,
            "system",
            "LLM/runner error: HTTP 400 Bad Request",
            extra={"no_api": True, "runner_error": True},
        )
        db.append_event(
            event_id=_ulid_like(),
            thread_id=tid,
            type_="stream.close",
            payload={},
            invoke_id=invoke_id,
        )
        append_recovery_notice(
            db,
            tid,
            "Recovery: auto-continue stopped.\nDecision: stop (bad_request).",
            extra={
                "auto_continue": True,
                "action": "stopped",
                "trigger_msg_id": user_msg_id,
                "source_msg_id": error_msg_id,
                "decision_category": "bad_request",
            },
        )

        assert discover_runner_actionable(db, tid) is None
        diagnosis = diagnose_thread(db, tid)
        assert diagnosis.is_healthy is False
        assert diagnosis.suggested_continue_point == user_msg_id
        assert diagnosis.details["stuck_ra1_trigger_msg_id"] == user_msg_id

        result = continue_thread_manually(db, tid)
        assert result.success is True
        assert result.continue_from_msg_id == user_msg_id
        assert error_msg_id in result.skipped_msg_ids
        assert user_msg_id not in result.skipped_msg_ids

        messages = create_snapshot(db, tid)["messages"]
        notices = [msg for msg in messages if msg.get("recovery_notice")]
        assert any("auto-continue stopped" in msg.get("content", "") for msg in notices)
        assert any(
            "manual /continue" in msg.get("content", "")
            and "Previous error: LLM/runner error: HTTP 400 Bad Request" in msg.get("content", "")
            for msg in notices
        )

        ra = discover_runner_actionable(db, tid)
        assert ra is not None
        assert ra.kind == "RA1_llm"
        assert ra.msg_id == user_msg_id

    def test_manual_continue_retries_user_after_cancel_with_recovery_notice(self, tmp_path):
        """A local recovery notice after a cancelled pending RA1 must not hide U1."""
        db, _ = _make_temp_db(tmp_path)
        tid = create_root_thread(db, name="test")

        user_msg_id = append_message(db, tid, "user", "Hello")
        interrupt_thread(db, tid, reason="user cancelled")
        append_recovery_notice(
            db,
            tid,
            "Recovery: auto-continue stopped.\nDetail: interrupted before retry.",
            extra={
                "auto_continue": True,
                "action": "stopped",
                "trigger_msg_id": user_msg_id,
                "stop_reason": "interrupted",
            },
        )

        assert discover_runner_actionable(db, tid) is None
        result = continue_thread_manually(db, tid)

        assert result.success is True
        assert result.continue_from_msg_id == user_msg_id
        ra = discover_runner_actionable(db, tid)
        assert ra is not None
        assert ra.kind == "RA1_llm"
        assert ra.msg_id == user_msg_id

    def test_manual_continue_does_not_replay_user_after_successful_assistant(self, tmp_path):
        db, _ = _make_temp_db(tmp_path)
        tid = create_root_thread(db, name="test")

        append_message(db, tid, "user", "Hello")
        append_message(db, tid, "assistant", "Hi")

        diagnosis = diagnose_thread(db, tid)
        assert diagnosis.is_healthy is True
        result = continue_thread(db, tid)
        assert result.success is True
        assert result.continue_from_msg_id is None


class TestSkippedMessages:
    """Tests for skipped message handling."""

    def test_skipped_assistant_does_not_block_ra1(self, tmp_path):
        """Assistant message marked as skipped should not block RA1."""
        db, _ = _make_temp_db(tmp_path)
        tid = create_root_thread(db, name="test")

        # Add user message
        append_message(db, tid, "user", "Hello")

        # Simulate partial assistant response with tool calls
        assistant_msg_id = _ulid_like()
        db.append_event(
            event_id=_ulid_like(),
            thread_id=tid,
            type_='msg.create',
            payload={
                'role': 'assistant',
                'content': 'Let me help...',
                'tool_calls': [{'id': 'tc1', 'function': {'name': 'test', 'arguments': '{}'}}],
            },
            msg_id=assistant_msg_id,
        )

        # Mark assistant message as skipped
        db.append_event(
            event_id=_ulid_like(),
            thread_id=tid,
            type_='msg.edit',
            payload={'skipped_on_continue': True},
            msg_id=assistant_msg_id,
        )

        # Add continue interrupt to reset boundary
        user_msg_id = db.conn.execute(
            "SELECT msg_id FROM events WHERE thread_id=? AND type='msg.create' AND payload_json LIKE '%user%'",
            (tid,),
        ).fetchone()[0]

        db.append_event(
            event_id=_ulid_like(),
            thread_id=tid,
            type_='control.interrupt',
            payload={'reason': 'continue', 'purpose': 'continue', 'continue_from_msg_id': user_msg_id},
        )

        # RA1 should trigger (skipped assistant with tool calls should not block)
        ra = discover_runner_actionable(db, tid)
        assert ra is not None, "RA1 should trigger despite skipped assistant message"
        assert ra.kind == "RA1_llm"


class TestDiagnoseAndContinue:
    """Integration tests for diagnose_thread + continue_thread flow."""

    def test_manual_continue_notice_has_local_recovery_flags(self, tmp_path):
        """Manual /continue status is persisted locally and hidden from APIs."""
        db, _ = _make_temp_db(tmp_path)
        tid = create_root_thread(db, name="test")

        user_msg_id = append_message(db, tid, "user", "Hello")
        append_message(db, tid, "assistant", "partial")
        append_message(db, tid, "system", "LLM/runner error: provider exploded")

        result = continue_thread(db, tid, user_msg_id)
        assert result.success is True
        notice_id = append_continue_recovery_notice(db, tid, result)

        messages = create_snapshot(db, tid)["messages"]
        notice = next(msg for msg in messages if msg.get("msg_id") == notice_id)
        assert notice["role"] == "system"
        assert notice["no_api"] is True
        assert notice["recovery_notice"] is True
        assert notice["preserve_on_continue"] is True
        assert "manual /continue" in notice["content"]
        assert "Skipped: 2 messages" in notice["content"]
        assert "assistant=1" in notice["content"]
        assert "system=1" in notice["content"]
        assert "Previous error: LLM/runner error: provider exploded" in notice["content"]

    def test_recovery_notice_survives_later_continue(self, tmp_path):
        """Local recovery notices are not skipped by subsequent continues."""
        db, _ = _make_temp_db(tmp_path)
        tid = create_root_thread(db, name="test")

        first_user = append_message(db, tid, "user", "First")
        append_message(db, tid, "assistant", "First answer")
        first = continue_thread(db, tid, first_user)
        assert first.success is True
        notice_id = append_continue_recovery_notice(db, tid, first)

        second_user = append_message(db, tid, "user", "Second")
        append_message(db, tid, "assistant", "Second answer")
        second = continue_thread(db, tid, first_user)
        assert second.success is True
        assert notice_id not in second.skipped_msg_ids
        assert second_user in second.skipped_msg_ids

        messages = create_snapshot(db, tid)["messages"]
        assert any(msg.get("msg_id") == notice_id for msg in messages)

    def test_diagnose_ignores_recovery_notice_error_text(self, tmp_path):
        """Recovery notices are not interpreted as provider/system errors."""
        db, _ = _make_temp_db(tmp_path)
        tid = create_root_thread(db, name="test")

        append_message(db, tid, "user", "Hello")
        append_message(db, tid, "assistant", "Hi")
        append_recovery_notice(db, tid, "Recovery note mentioning LLM/runner error: old failure")

        diagnosis = diagnose_thread(db, tid)
        assert diagnosis.is_healthy is True
        assert "last_error" not in diagnosis.details

        result = continue_thread(db, tid)
        assert result.success is True
        assert result.skipped_msg_ids == []

    def test_diagnose_ignores_assistant_notes_for_consecutive_assistant_check(self, tmp_path):
        """Assistant notes are provider-hidden and can appear between tool calls."""
        db, _ = _make_temp_db(tmp_path)
        tid = create_root_thread(db, name="test")

        append_message(db, tid, "user", "Keep me updated while working")
        tool_call_id = "call-answer-note"
        append_message(
            db,
            tid,
            "assistant",
            "",
            extra={
                "tool_calls": [
                    {
                        "id": tool_call_id,
                        "type": "function",
                        "function": {
                            "name": "answer_user_while_preserving_llm_turn",
                            "arguments": '{"message":"Still working"}',
                        },
                    }
                ]
            },
        )
        note_1 = append_message(
            db,
            tid,
            "assistant",
            "Still working",
            extra={"answer_user_preserve_turn": True, "tool_call_id": tool_call_id},
        )
        note_2 = append_message(
            db,
            tid,
            "assistant",
            "Still working more",
            extra={"answer_user_preserve_turn": True, "tool_call_id": tool_call_id},
        )
        append_message(db, tid, "tool", "Interim answer shown to user.", extra={"tool_call_id": tool_call_id})

        diagnosis = diagnose_thread(db, tid)
        assert diagnosis.is_healthy is True
        assert "consecutive_assistants" not in diagnosis.details

        result = continue_thread(db, tid)
        assert result.success is True
        assert result.skipped_msg_ids == []

        messages = create_snapshot(db, tid)["messages"]
        assert any(msg.get("msg_id") == note_1 for msg in messages)
        assert any(msg.get("msg_id") == note_2 for msg in messages)

    def test_diagnose_detects_unclosed_stream(self, tmp_path):
        """diagnose_thread should detect unclosed streams."""
        db, _ = _make_temp_db(tmp_path)
        tid = create_root_thread(db, name="test")
        append_message(db, tid, "user", "Hello")

        # Simulate unclosed stream (stream.open without stream.close)
        # Note: stream.open requires both invoke_id and msg_id
        invoke_id = _ulid_like()
        msg_id = _ulid_like()
        db.append_event(
            event_id=_ulid_like(),
            thread_id=tid,
            type_='stream.open',
            payload={'stream_kind': 'llm'},
            invoke_id=invoke_id,
            msg_id=msg_id,
        )

        diagnosis = diagnose_thread(db, tid)
        assert diagnosis.is_healthy is False
        assert 'unclosed_streams' in diagnosis.details
        assert diagnosis.suggested_continue_point is not None

    def test_continue_thread_fixes_unclosed_stream(self, tmp_path):
        """continue_thread should allow retry after unclosed stream."""
        db, _ = _make_temp_db(tmp_path)
        tid = create_root_thread(db, name="test")
        append_message(db, tid, "user", "Hello")

        # Simulate unclosed stream
        # Note: stream.open requires both invoke_id and msg_id
        invoke_id = _ulid_like()
        msg_id = _ulid_like()
        db.append_event(
            event_id=_ulid_like(),
            thread_id=tid,
            type_='stream.open',
            payload={'stream_kind': 'llm'},
            invoke_id=invoke_id,
            msg_id=msg_id,
        )

        # Diagnose and continue
        diagnosis = diagnose_thread(db, tid)
        result = continue_thread(db, tid, diagnosis.suggested_continue_point)
        assert result.success is True

        # RA1 should now trigger
        ra = discover_runner_actionable(db, tid)
        assert ra is not None
        assert ra.kind == "RA1_llm"


class TestThreadRecoverySettings:
    def test_recovery_auto_continue_defaults_enabled(self, tmp_path):
        db, _ = _make_temp_db(tmp_path)
        tid = create_root_thread(db, name="test")

        assert get_thread_recovery(db, tid).auto_continue_on_error is True

    def test_recovery_auto_continue_can_be_set(self, tmp_path):
        db, _ = _make_temp_db(tmp_path)
        tid = create_root_thread(db, name="test")

        set_thread_recovery(db, tid, auto_continue_on_error=False)
        assert get_thread_recovery(db, tid).auto_continue_on_error is False

        set_thread_recovery(db, tid, auto_continue_on_error=True)
        assert get_thread_recovery(db, tid).auto_continue_on_error is True

    def test_recovery_settings_inherit_from_nearest_ancestor(self, tmp_path):
        db, _ = _make_temp_db(tmp_path)
        root = create_root_thread(db, name="root")
        child = create_child_thread(db, root, name="child")
        grandchild = create_child_thread(db, child, name="grandchild")

        set_thread_recovery(db, root, auto_continue_on_error=False)
        assert get_thread_recovery(db, child).auto_continue_on_error is False
        assert get_thread_recovery(db, grandchild).auto_continue_on_error is False

        set_thread_recovery(db, child, auto_continue_on_error=True)
        assert get_thread_recovery(db, child).auto_continue_on_error is True
        assert get_thread_recovery(db, grandchild).auto_continue_on_error is True


class TestContinuePointWithPendingToolCalls:
    """Regression tests for continue-point selection around TC4/TC5 state."""

    def test_find_continue_point_skips_before_unpublished_tool_parent(self, tmp_path):
        """continue_thread should remove stale unpublished tool calls from state.

        If a thread has a later user retry prompt but an older assistant tool call
        is still stuck in TC4/TC5, the continue point must move to *before* the
        assistant parent message. Otherwise the stale tool call survives recovery
        and blocks RA1.
        """
        db, _ = _make_temp_db(tmp_path)
        tid = create_root_thread(db, name="test")

        user_msg_id = append_message(db, tid, "user", "Solve it")

        # Assistant tool call that finishes but never receives output approval.
        assistant_msg_id = _ulid_like()
        tool_call_id = "tc-pending"
        db.append_event(
            event_id=_ulid_like(),
            thread_id=tid,
            type_='msg.create',
            msg_id=assistant_msg_id,
            payload={
                'role': 'assistant',
                'content': 'Checking something...',
                'tool_calls': [
                    {
                        'id': tool_call_id,
                        'type': 'function',
                        'function': {'name': 'bash', 'arguments': '{}'},
                    }
                ],
            },
        )
        db.append_event(
            event_id=_ulid_like(),
            thread_id=tid,
            type_='tool_call.approval',
            payload={'tool_call_id': tool_call_id, 'decision': 'granted'},
        )
        db.append_event(
            event_id=_ulid_like(),
            thread_id=tid,
            type_='tool_call.execution_started',
            payload={'tool_call_id': tool_call_id},
        )
        db.append_event(
            event_id=_ulid_like(),
            thread_id=tid,
            type_='tool_call.finished',
            payload={'tool_call_id': tool_call_id, 'reason': 'success', 'output': 'big output'},
        )

        retry_msg_id = append_message(db, tid, "user", "Please write solution.py now")

        diagnosis = diagnose_thread(db, tid)
        assert diagnosis.is_healthy is False
        assert diagnosis.suggested_continue_point == user_msg_id

        result = continue_thread(db, tid, diagnosis.suggested_continue_point)
        assert result.success is True

        ra = discover_runner_actionable(db, tid)
        assert ra is not None
        assert ra.kind == "RA1_llm"
        assert ra.msg_id == user_msg_id


class TestWaitThreadSettled:
    def test_wait_thread_settled_distinguishes_interrupted_output_approval(self, tmp_path):
        db, _ = _make_temp_db(tmp_path)
        tid = create_root_thread(db, name="test")

        assistant_msg_id = _ulid_like()
        tool_call_id = "tc1"
        db.append_event(
            event_id=_ulid_like(),
            thread_id=tid,
            type_='msg.create',
            msg_id=assistant_msg_id,
            payload={
                'role': 'assistant',
                'content': 'Running tool',
                'tool_calls': [
                    {
                        'id': tool_call_id,
                        'type': 'function',
                        'function': {'name': 'bash', 'arguments': '{}'},
                    }
                ],
            },
        )
        db.append_event(
            event_id=_ulid_like(),
            thread_id=tid,
            type_='tool_call.approval',
            payload={'tool_call_id': tool_call_id, 'decision': 'granted'},
        )
        db.append_event(
            event_id=_ulid_like(),
            thread_id=tid,
            type_='tool_call.execution_started',
            payload={'tool_call_id': tool_call_id},
        )
        db.append_event(
            event_id=_ulid_like(),
            thread_id=tid,
            type_='tool_call.finished',
            payload={'tool_call_id': tool_call_id, 'reason': 'interrupted', 'output': 'incomplete'},
        )

        import asyncio

        state = asyncio.run(wait_thread_settled(db, tid, poll_sec=0.01, quiet_checks=1))
        assert state == 'waiting_output_approval'


class TestLeaseTakeover:
    """Tests for try_open_stream lease takeover functionality."""

    def test_try_open_stream_takes_over_expired_lease(self, tmp_path):
        """try_open_stream should take over an expired lease instead of failing."""
        db, _ = _make_temp_db(tmp_path)
        tid = create_root_thread(db, name="test")

        # Simulate an expired lease (expired 1 minute ago)
        expired_time = (_utcnow() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
        db.conn.execute(
            "INSERT INTO open_streams (thread_id, invoke_id, lease_until, owner, purpose) VALUES (?, ?, ?, ?, ?)",
            (tid, "old-invoke", expired_time, "crashed-process", "llm"),
        )

        # try_open_stream should take over the expired lease
        new_lease = (_utcnow() + timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
        result = db.try_open_stream(tid, "new-invoke", new_lease, owner="new-process", purpose="llm")
        assert result is True

        # Verify the lease was updated
        row = db.current_open(tid)
        assert row is not None
        assert row["invoke_id"] == "new-invoke"
        assert row["owner"] == "new-process"

        interrupt = db.conn.execute(
            "SELECT payload_json FROM events WHERE thread_id=? AND type='control.interrupt' ORDER BY event_seq DESC LIMIT 1",
            (tid,),
        ).fetchone()
        assert interrupt is not None
        payload = json.loads(interrupt[0])
        assert payload["reason"] == "expired_lease_takeover"
        assert payload["old_invoke_id"] == "old-invoke"
        assert payload["new_invoke_id"] == "new-invoke"
        assert payload["purpose"] == "llm"

    def test_try_open_stream_fails_on_active_lease(self, tmp_path):
        """try_open_stream should fail if there's an active lease."""
        db, _ = _make_temp_db(tmp_path)
        tid = create_root_thread(db, name="test")

        # Simulate an active lease (expires in 1 minute)
        active_time = (_utcnow() + timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
        db.conn.execute(
            "INSERT INTO open_streams (thread_id, invoke_id, lease_until, owner, purpose) VALUES (?, ?, ?, ?, ?)",
            (tid, "active-invoke", active_time, "running-process", "llm"),
        )

        # try_open_stream should fail
        new_lease = (_utcnow() + timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S")
        result = db.try_open_stream(tid, "new-invoke", new_lease, owner="new-process", purpose="llm")
        assert result is False

        # Original lease should still be there
        row = db.current_open(tid)
        assert row is not None
        assert row["invoke_id"] == "active-invoke"

    def test_try_open_stream_inserts_new_lease(self, tmp_path):
        """try_open_stream should insert a new lease when none exists."""
        db, _ = _make_temp_db(tmp_path)
        tid = create_root_thread(db, name="test")

        # No existing lease
        assert db.current_open(tid) is None

        # try_open_stream should insert a new lease
        new_lease = (_utcnow() + timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
        result = db.try_open_stream(tid, "new-invoke", new_lease, owner="new-process", purpose="llm")
        assert result is True

        # Verify the lease was inserted
        row = db.current_open(tid)
        assert row is not None
        assert row["invoke_id"] == "new-invoke"


def test_validate_continue_target_is_side_effect_free_and_noarg_is_not_diagnosed(tmp_path):
    db, _ = _make_temp_db(tmp_path)
    tid = create_root_thread(db, name="validate")
    msg_id = append_message(db, tid, "user", "anchor")
    before = db.max_event_seq(tid)

    valid = validate_continue_target(db, tid, msg_id)
    missing = validate_continue_target(db, tid, "missing")
    automatic = validate_continue_target(db, tid, None)

    assert valid.success is True
    assert valid.continue_from_msg_id == msg_id
    assert missing.success is False
    assert missing.message == "Message not found: missing"
    assert automatic.success is True
    assert automatic.continue_from_msg_id is None
    assert automatic.diagnosis is None
    assert db.max_event_seq(tid) == before


def test_explicit_invalid_continue_preflight_beats_active_lease(tmp_path):
    db, _ = _make_temp_db(tmp_path)
    tid = create_root_thread(db, name="validate-live")
    append_message(db, tid, "user", "anchor")
    assert db.try_open_stream(
        tid, "invoke-live-validation", "2999-01-01 00:00:00", owner="test", purpose="tool"
    )
    before = db.max_event_seq(tid)
    lease = dict(db.current_open(tid))

    result = continue_thread(db, tid, msg_id="missing")

    assert result.success is False
    assert result.message == "Message not found: missing"
    assert db.max_event_seq(tid) == before
    assert dict(db.current_open(tid)) == lease
