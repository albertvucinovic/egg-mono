"""Tests for continue_thread recovery functionality.

These tests verify that crash recovery works correctly:
1. Threads with expired leases can be continued
2. RA1 boundary is reset after continue_thread
3. Skipped messages don't block RA1
4. list_active_threads respects lease expiration
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from eggthreads import (
    ThreadsDB,
    create_root_thread,
    append_message,
    diagnose_thread,
    continue_thread,
    is_thread_continuable,
    wait_thread_settled,
)
from eggthreads.api import list_active_threads, collect_subtree
from eggthreads.tool_state import discover_runner_actionable, _last_stream_close_seq


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
        expired_time = (datetime.utcnow() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
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
        active_time = (datetime.utcnow() + timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
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
        expired_time = (datetime.utcnow() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
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
        expired_time = (datetime.utcnow() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
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
    def test_wait_thread_settled_distinguishes_waiting_output_approval(self, tmp_path):
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
            payload={'tool_call_id': tool_call_id, 'reason': 'success', 'output': 'out'},
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
        expired_time = (datetime.utcnow() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
        db.conn.execute(
            "INSERT INTO open_streams (thread_id, invoke_id, lease_until, owner, purpose) VALUES (?, ?, ?, ?, ?)",
            (tid, "old-invoke", expired_time, "crashed-process", "llm"),
        )

        # try_open_stream should take over the expired lease
        new_lease = (datetime.utcnow() + timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
        result = db.try_open_stream(tid, "new-invoke", new_lease, owner="new-process", purpose="llm")
        assert result is True

        # Verify the lease was updated
        row = db.current_open(tid)
        assert row is not None
        assert row["invoke_id"] == "new-invoke"
        assert row["owner"] == "new-process"

    def test_try_open_stream_fails_on_active_lease(self, tmp_path):
        """try_open_stream should fail if there's an active lease."""
        db, _ = _make_temp_db(tmp_path)
        tid = create_root_thread(db, name="test")

        # Simulate an active lease (expires in 1 minute)
        active_time = (datetime.utcnow() + timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
        db.conn.execute(
            "INSERT INTO open_streams (thread_id, invoke_id, lease_until, owner, purpose) VALUES (?, ?, ?, ?, ?)",
            (tid, "active-invoke", active_time, "running-process", "llm"),
        )

        # try_open_stream should fail
        new_lease = (datetime.utcnow() + timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S")
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
        new_lease = (datetime.utcnow() + timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
        result = db.try_open_stream(tid, "new-invoke", new_lease, owner="new-process", purpose="llm")
        assert result is True

        # Verify the lease was inserted
        row = db.current_open(tid)
        assert row is not None
        assert row["invoke_id"] == "new-invoke"
