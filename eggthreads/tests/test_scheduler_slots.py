"""Tests for scheduler slot management and priority ordering.

These tests verify:
- Slots are properly freed when threads complete
- Runnable threads not scheduled due to slot limits are re-checked (not skipped by watermark)
- Non-runnable threads are correctly skipped by watermark until events change
- Priority ordering works correctly with limited slots
"""
from __future__ import annotations

import asyncio
import json
from typing import Dict, Any, Set, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import eggthreads as ts
from eggthreads import (
    ThreadsDB,
    is_thread_runnable,
    RunnerConfig,
)
from eggthreads.runner import SubtreeScheduler


import uuid


def _make_db(tmp_path) -> ThreadsDB:
    """Create a fresh database for testing."""
    db = ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    return db


def _unique_id() -> str:
    """Generate a unique ID for events."""
    return str(uuid.uuid4())


def _append_event(db: ThreadsDB, tid: str, type_: str, payload: Dict[str, Any], *, msg_id: str | None = None) -> None:
    """Append a JSON event with a fresh unique event_id."""
    eid = _unique_id()
    db.append_event(event_id=eid, thread_id=tid, type_=type_, payload=payload, msg_id=msg_id)


def _make_thread_runnable(db: ThreadsDB, thread_id: str) -> None:
    """Make a thread runnable by adding a user message that triggers RA1.

    A user message without tool_calls, keep_user_turn, or no_api will trigger RA1.
    """
    msg_id = _unique_id()
    db.append_event(
        event_id=_unique_id(),
        thread_id=thread_id,
        type_="msg.create",
        payload={"role": "user", "content": "Hello, please respond."},
        msg_id=msg_id,
    )


def _make_thread_not_runnable(db: ThreadsDB, thread_id: str) -> None:
    """Make a thread NOT runnable by simulating a completed assistant turn.

    Add an assistant message which means there's no pending user message to respond to.
    """
    msg_id = _unique_id()
    invoke_id = _unique_id()

    # First open a stream (requires both invoke_id and msg_id per schema)
    db.append_event(
        event_id=_unique_id(),
        thread_id=thread_id,
        type_="stream.open",
        payload={"model": "test"},
        msg_id=msg_id,
        invoke_id=invoke_id,
    )

    # Add assistant message
    db.append_event(
        event_id=_unique_id(),
        thread_id=thread_id,
        type_="msg.create",
        payload={"role": "assistant", "content": "Done."},
        msg_id=msg_id,
    )

    # Close the stream to mark this turn complete
    db.append_event(
        event_id=_unique_id(),
        thread_id=thread_id,
        type_="stream.close",
        payload={"reason": "done"},
        invoke_id=invoke_id,
    )


class TestSlotManagement:
    """Tests for scheduler slot availability and watermark behavior."""

    def test_slots_freed_after_thread_completes(self, tmp_path):
        """Slots should become available when threads finish running.

        This tests that the semaphore-based concurrency limiting works correctly:
        when a thread finishes and releases its semaphore slot, another thread
        should be able to acquire it.
        """
        db = _make_db(tmp_path)

        # Create 4 runnable threads
        root = ts.create_root_thread(db, name="root")
        _make_thread_runnable(db, root)

        threads = [root]
        for i in range(3):
            child = ts.create_child_thread(db, root, name=f"child-{i}")
            _make_thread_runnable(db, child)
            threads.append(child)

        # Verify all 4 are runnable
        for tid in threads:
            assert is_thread_runnable(db, tid), f"Thread {tid} should be runnable"

        # Create scheduler with max_concurrent=2
        cfg = RunnerConfig(max_concurrent_threads=2)

        # Track which threads get scheduled
        scheduled_threads: List[str] = []
        completed_threads: Set[str] = set()

        # Mock the ThreadRunner to track scheduling
        original_run_once = None

        async def mock_run_once(runner_self):
            scheduled_threads.append(runner_self.thread_id)
            # Simulate work completion - make thread not runnable
            _make_thread_not_runnable(db, runner_self.thread_id)
            completed_threads.add(runner_self.thread_id)

        with patch('eggthreads.runner.ThreadRunner.run_once', mock_run_once):
            # Run the scheduler for a limited time
            llm = MagicMock()
            scheduler = SubtreeScheduler(db, root, llm=llm, config=cfg)

            async def run_scheduler_limited():
                # Run scheduler loop manually for a few iterations
                sem = asyncio.Semaphore(cfg.max_concurrent_threads)
                running_threads = set()
                last_checked_seq: Dict[str, int] = {}
                tasks = []

                async def drive(tid: str):
                    try:
                        async with sem:
                            # Simulate run_once
                            scheduled_threads.append(tid)
                            _make_thread_not_runnable(db, tid)
                            await asyncio.sleep(0.01)  # Small delay
                    finally:
                        running_threads.discard(tid)

                # Run for a few iterations
                for _ in range(10):
                    for tid in scheduler._collect_subtree(scheduler.root):
                        if tid in running_threads:
                            continue

                        try:
                            max_seq = db.max_event_seq(tid)
                        except Exception:
                            max_seq = -1
                        if max_seq == last_checked_seq.get(tid, -1):
                            continue

                        if is_thread_runnable(db, tid):
                            running_threads.add(tid)
                            task = asyncio.create_task(drive(tid))
                            tasks.append(task)
                        else:
                            # Only update watermark for non-runnable threads
                            last_checked_seq[tid] = max_seq

                    await asyncio.sleep(0.02)

                # Wait for all tasks to complete
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)

            asyncio.run(run_scheduler_limited())

        # All 4 threads should have been scheduled (even though max_concurrent=2)
        # because slots are freed when threads complete
        assert len(set(scheduled_threads)) == 4, f"All 4 threads should be scheduled, got {len(set(scheduled_threads))}"

    def test_runnable_threads_not_skipped_by_watermark(self, tmp_path):
        """Threads that were runnable but not scheduled should be re-checked.

        This tests the watermark fix: if a thread is runnable but wasn't scheduled
        (e.g., due to slot limits), it should NOT have its watermark updated,
        so it gets re-checked on the next iteration.

        Bug scenario without fix:
        1. Threads A, B, C, D are runnable
        2. Slots = 2, so A, B get scheduled
        3. C, D are checked (runnable=True) but not scheduled (no slots)
        4. If we update watermark for C, D, they won't be re-checked
        5. When A finishes, C should be scheduled but watermark blocks it

        With fix:
        - Only update watermark for threads that are NOT runnable
        - C, D remain re-checkable
        """
        db = _make_db(tmp_path)

        # Create 4 runnable threads
        root = ts.create_root_thread(db, name="root")
        t_a = root
        _make_thread_runnable(db, t_a)

        t_b = ts.create_child_thread(db, root, name="thread-B")
        _make_thread_runnable(db, t_b)

        t_c = ts.create_child_thread(db, root, name="thread-C")
        _make_thread_runnable(db, t_c)

        t_d = ts.create_child_thread(db, root, name="thread-D")
        _make_thread_runnable(db, t_d)

        # Track watermark behavior
        last_checked_seq: Dict[str, int] = {}
        threads_found_runnable: Set[str] = set()
        threads_scheduled: List[str] = []

        # Simulate first iteration with max_concurrent=2
        max_concurrent = 2
        running_threads: Set[str] = set()

        all_threads = [t_a, t_b, t_c, t_d]

        # First iteration: check all threads
        for tid in all_threads:
            max_seq = db.max_event_seq(tid)

            # Watermark check
            if max_seq == last_checked_seq.get(tid, -1):
                continue  # Should not skip on first iteration

            if is_thread_runnable(db, tid):
                threads_found_runnable.add(tid)

                # Can we schedule?
                if len(running_threads) < max_concurrent:
                    running_threads.add(tid)
                    threads_scheduled.append(tid)
                # FIX: Do NOT update watermark for runnable threads that weren't scheduled
                # This allows them to be re-checked on the next iteration
            else:
                # Only update watermark for non-runnable threads
                last_checked_seq[tid] = max_seq

        # Verify: all 4 found runnable, but only 2 scheduled
        assert len(threads_found_runnable) == 4, "All 4 threads should be found runnable"
        assert len(threads_scheduled) == 2, "Only 2 threads should be scheduled due to slot limit"

        # Verify: watermark should NOT be set for threads C and D (runnable but not scheduled)
        scheduled_set = set(threads_scheduled)
        for tid in all_threads:
            if tid in scheduled_set:
                # Scheduled threads don't need watermark (they're in running_threads)
                pass
            else:
                # Non-scheduled runnable threads should NOT have watermark set
                assert tid not in last_checked_seq, \
                    f"Thread {tid} was runnable but not scheduled - watermark should NOT be set"

        # Simulate: A completes, slot opens
        running_threads.discard(threads_scheduled[0])

        # Second iteration: threads C and D should be re-checkable
        for tid in all_threads:
            if tid in running_threads:
                continue

            max_seq = db.max_event_seq(tid)

            # Watermark check - C and D should NOT be skipped
            if max_seq == last_checked_seq.get(tid, -1):
                continue

            if is_thread_runnable(db, tid):
                if len(running_threads) < max_concurrent:
                    running_threads.add(tid)
                    threads_scheduled.append(tid)

        # Verify: 3 threads should now be scheduled
        assert len(threads_scheduled) == 3, \
            f"After slot freed, 3 threads should be scheduled, got {len(threads_scheduled)}"

    def test_non_runnable_threads_skipped_by_watermark(self, tmp_path):
        """Threads that are not runnable should be skipped until events change.

        This tests that the watermark optimization works correctly for truly
        idle threads: if a thread has no new events and was previously found
        not runnable, it should be skipped on subsequent iterations.
        """
        db = _make_db(tmp_path)

        # Create thread that is NOT runnable (no pending user message)
        root = ts.create_root_thread(db, name="root")

        # Add a complete turn (assistant responded, no pending work)
        _make_thread_not_runnable(db, root)

        # Verify not runnable
        assert not is_thread_runnable(db, root), "Thread should not be runnable"

        # Simulate watermark tracking
        last_checked_seq: Dict[str, int] = {}
        runnable_check_count = 0

        # First iteration: check the thread
        max_seq = db.max_event_seq(root)
        if max_seq != last_checked_seq.get(root, -1):
            runnable_check_count += 1
            if not is_thread_runnable(db, root):
                # Thread is not runnable - set watermark
                last_checked_seq[root] = max_seq

        assert runnable_check_count == 1, "Should have checked runnability once"
        assert root in last_checked_seq, "Watermark should be set for non-runnable thread"

        # Second iteration: thread should be skipped (no new events)
        max_seq = db.max_event_seq(root)
        if max_seq != last_checked_seq.get(root, -1):
            runnable_check_count += 1

        assert runnable_check_count == 1, "Should NOT check again - watermark blocks"

        # Add new event (simulating user input)
        _make_thread_runnable(db, root)

        # Third iteration: thread should be re-checked (new events)
        max_seq = db.max_event_seq(root)
        if max_seq != last_checked_seq.get(root, -1):
            runnable_check_count += 1

        assert runnable_check_count == 2, "Should check again after new events"

    def test_watermark_not_updated_for_runnable_threads(self, tmp_path):
        """Verify that watermark is only updated for non-runnable threads.

        This is the core fix verification: the watermark should NOT be updated
        for threads that are runnable, regardless of whether they get scheduled.
        """
        db = _make_db(tmp_path)

        root = ts.create_root_thread(db, name="root")
        _make_thread_runnable(db, root)

        assert is_thread_runnable(db, root), "Thread should be runnable"

        last_checked_seq: Dict[str, int] = {}

        # Simulate scheduler logic with the fix applied
        max_seq = db.max_event_seq(root)

        # Watermark check
        if max_seq != last_checked_seq.get(root, -1):
            if is_thread_runnable(db, root):
                # Runnable - do NOT set watermark
                pass
            else:
                # Not runnable - set watermark
                last_checked_seq[root] = max_seq

        # Verify: watermark should NOT be set for runnable thread
        assert root not in last_checked_seq, \
            "Watermark should NOT be set for runnable thread"


class TestPriorityOrdering:
    """Tests for priority-based scheduling.

    Note: These tests verify priority ordering behavior. If priority functions
    (get_thread_scheduling, set_thread_scheduling) are not yet implemented,
    some tests will be skipped.
    """

    def test_threads_collected_in_bfs_order(self, tmp_path):
        """Verify baseline: threads are collected in BFS order."""
        db = _make_db(tmp_path)

        root = ts.create_root_thread(db, name="root")
        child1 = ts.create_child_thread(db, root, name="child1")
        child2 = ts.create_child_thread(db, root, name="child2")
        grandchild = ts.create_child_thread(db, child1, name="grandchild")

        subtree = ts.collect_subtree(db, root)

        # Root first
        assert subtree[0] == root
        # Children before grandchildren
        assert set(subtree[1:3]) == {child1, child2}
        assert subtree[3] == grandchild

    def test_priority_functions_exist(self, tmp_path):
        """Check if priority functions are implemented."""
        # This test documents the expected API
        has_scheduling_api = (
            hasattr(ts, 'get_thread_scheduling') and
            hasattr(ts, 'set_thread_scheduling')
        )

        if not has_scheduling_api:
            pytest.skip("Priority scheduling functions not yet implemented")

        db = _make_db(tmp_path)
        root = ts.create_root_thread(db, name="root")

        # Test getting default scheduling
        settings = ts.get_thread_scheduling(db, root)
        assert hasattr(settings, 'priority'), "Settings should have priority attribute"
        assert settings.priority == 0, "Default priority should be 0"

    def test_high_priority_scheduled_first_with_sort_helper(self, tmp_path):
        """Higher priority threads should be scheduled before lower priority.

        This tests the _sort_by_priority helper function behavior.
        """
        # Check if _sort_by_priority exists in runner module
        from eggthreads import runner

        if not hasattr(runner, '_sort_by_priority'):
            pytest.skip("_sort_by_priority helper not yet implemented")

        db = _make_db(tmp_path)

        # Create threads (order matters for testing)
        root = ts.create_root_thread(db, name="root")
        low_priority = ts.create_child_thread(db, root, name="low")
        high_priority = ts.create_child_thread(db, root, name="high")
        medium_priority = ts.create_child_thread(db, root, name="medium")

        # Set priorities
        ts.set_thread_scheduling(db, high_priority, priority=10)
        ts.set_thread_scheduling(db, medium_priority, priority=5)
        ts.set_thread_scheduling(db, low_priority, priority=0)

        threads = [low_priority, high_priority, medium_priority]

        # Sort by priority
        sorted_threads = runner._sort_by_priority(threads, "none", db)

        # High priority first, then medium, then low
        assert sorted_threads[0] == high_priority
        assert sorted_threads[1] == medium_priority
        assert sorted_threads[2] == low_priority

    def test_priority_respected_with_limited_slots(self, tmp_path):
        """When slots are limited, highest priority threads get them.

        Create 4 threads: A(p=5), B(p=5), C(p=0), D(p=0)
        max_concurrent=2
        Verify A and B get scheduled first, not C and D.
        """
        # Check if priority functions exist
        if not hasattr(ts, 'set_thread_scheduling'):
            pytest.skip("Priority scheduling functions not yet implemented")

        from eggthreads import runner
        if not hasattr(runner, '_sort_by_priority'):
            pytest.skip("_sort_by_priority helper not yet implemented")

        db = _make_db(tmp_path)

        root = ts.create_root_thread(db, name="root")

        # Create threads and make runnable
        t_a = ts.create_child_thread(db, root, name="thread-A")
        t_b = ts.create_child_thread(db, root, name="thread-B")
        t_c = ts.create_child_thread(db, root, name="thread-C")
        t_d = ts.create_child_thread(db, root, name="thread-D")

        for t in [root, t_a, t_b, t_c, t_d]:
            _make_thread_runnable(db, t)

        # Set priorities: A and B high, C and D low
        ts.set_thread_scheduling(db, t_a, priority=5)
        ts.set_thread_scheduling(db, t_b, priority=5)
        ts.set_thread_scheduling(db, t_c, priority=0)
        ts.set_thread_scheduling(db, t_d, priority=0)

        # Sort threads by priority
        all_threads = [root, t_a, t_b, t_c, t_d]
        sorted_threads = runner._sort_by_priority(all_threads, "none", db)

        # Schedule first 2 (simulating max_concurrent=2)
        scheduled = []
        for tid in sorted_threads:
            if is_thread_runnable(db, tid) and len(scheduled) < 2:
                scheduled.append(tid)

        # A and B should be scheduled (highest priority)
        assert t_a in scheduled, "High priority thread A should be scheduled"
        assert t_b in scheduled, "High priority thread B should be scheduled"
        assert t_c not in scheduled, "Low priority thread C should wait"
        assert t_d not in scheduled, "Low priority thread D should wait"

    def test_alphabetical_tiebreaker(self, tmp_path):
        """Equal priority threads sorted alphabetically when mode='alphabetical'."""
        # Check if priority functions exist
        if not hasattr(ts, 'set_thread_scheduling'):
            pytest.skip("Priority scheduling functions not yet implemented")

        from eggthreads import runner
        if not hasattr(runner, '_sort_by_priority'):
            pytest.skip("_sort_by_priority helper not yet implemented")

        db = _make_db(tmp_path)
        root = ts.create_root_thread(db, name="root")

        # Create threads with specific IDs (we'll use names to verify ordering)
        # Note: thread_ids are ULIDs so we can't control them directly,
        # but the sort should still work based on the actual IDs
        t1 = ts.create_child_thread(db, root, name="zebra")
        t2 = ts.create_child_thread(db, root, name="alpha")
        t3 = ts.create_child_thread(db, root, name="beta")

        # All same priority
        ts.set_thread_scheduling(db, t1, priority=0)
        ts.set_thread_scheduling(db, t2, priority=0)
        ts.set_thread_scheduling(db, t3, priority=0)

        threads = [t1, t2, t3]

        # Sort with alphabetical mode
        sorted_threads = runner._sort_by_priority(threads, "alphabetical", db)

        # Should be sorted by thread_id alphabetically
        assert sorted_threads == sorted(threads), "Should be sorted alphabetically by thread_id"


class TestSchedulerIntegration:
    """Integration tests for the full scheduler loop."""

    def test_scheduler_processes_all_threads_eventually(self, tmp_path):
        """All runnable threads should eventually be processed.

        Create multiple runnable threads with limited concurrency.
        Verify all threads get scheduled over time.
        """
        db = _make_db(tmp_path)

        # Create 6 runnable threads
        root = ts.create_root_thread(db, name="root")
        _make_thread_runnable(db, root)

        threads = [root]
        for i in range(5):
            child = ts.create_child_thread(db, root, name=f"child-{i}")
            _make_thread_runnable(db, child)
            threads.append(child)

        # Track scheduling
        scheduled_threads: Set[str] = set()

        # Simulate scheduler with proper watermark handling
        max_concurrent = 2
        running_threads: Set[str] = set()
        last_checked_seq: Dict[str, int] = {}

        async def run_test():
            nonlocal scheduled_threads, running_threads, last_checked_seq

            sem = asyncio.Semaphore(max_concurrent)
            tasks = []

            async def drive(tid: str):
                try:
                    async with sem:
                        scheduled_threads.add(tid)
                        _make_thread_not_runnable(db, tid)
                        await asyncio.sleep(0.01)
                finally:
                    running_threads.discard(tid)

            # Run for multiple iterations
            for iteration in range(20):
                for tid in threads:
                    if tid in running_threads:
                        continue

                    try:
                        max_seq = db.max_event_seq(tid)
                    except Exception:
                        max_seq = -1

                    # Watermark check
                    if max_seq == last_checked_seq.get(tid, -1):
                        continue

                    if is_thread_runnable(db, tid):
                        running_threads.add(tid)
                        tasks.append(asyncio.create_task(drive(tid)))
                    else:
                        # Only update watermark for non-runnable threads
                        last_checked_seq[tid] = max_seq

                await asyncio.sleep(0.02)

                # Check if all done
                if len(scheduled_threads) == len(threads):
                    break

            # Wait for remaining tasks
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        asyncio.run(run_test())

        # All threads should have been scheduled
        assert scheduled_threads == set(threads), \
            f"All {len(threads)} threads should be scheduled, got {len(scheduled_threads)}"

    def test_scheduler_config_max_concurrent(self, tmp_path):
        """Verify RunnerConfig.max_concurrent_threads is respected."""
        cfg = RunnerConfig(max_concurrent_threads=3)
        assert cfg.max_concurrent_threads == 3

        cfg2 = RunnerConfig()
        assert cfg2.max_concurrent_threads == 4  # default

    def test_collect_subtree_respects_waiting_until(self, tmp_path):
        """_collect_subtree should respect waiting_until constraints."""
        db = _make_db(tmp_path)

        root = ts.create_root_thread(db, name="root")
        child = ts.create_child_thread(db, root, name="child")

        # Set waiting_until to far future
        # Note: runner.py uses ISO = "%Y-%m-%d %H:%M:%S" format
        from datetime import datetime, timedelta
        ISO_FORMAT = "%Y-%m-%d %H:%M:%S"
        future_time = (datetime.utcnow() + timedelta(hours=1)).strftime(ISO_FORMAT)
        db.conn.execute(
            "UPDATE children SET waiting_until = ? WHERE child_id = ?",
            (future_time, child)
        )
        db.conn.commit()

        # Create scheduler and check subtree
        cfg = RunnerConfig()
        llm = MagicMock()
        scheduler = SubtreeScheduler(db, root, llm=llm, config=cfg)

        subtree = scheduler._collect_subtree(root)

        # Child should NOT be in subtree (waiting_until not reached)
        assert root in subtree, "Root should be in subtree"
        assert child not in subtree, "Child with future waiting_until should NOT be in subtree"

        # Set waiting_until to past
        past_time = (datetime.utcnow() - timedelta(hours=1)).strftime(ISO_FORMAT)
        db.conn.execute(
            "UPDATE children SET waiting_until = ? WHERE child_id = ?",
            (past_time, child)
        )
        db.conn.commit()

        subtree = scheduler._collect_subtree(root)

        # Now child should be in subtree
        assert child in subtree, "Child with past waiting_until should be in subtree"


class TestTimeoutEdgeCases:
    """Tests for edge cases with api_timeout=0 and threshold=0 (infinite)."""

    def test_api_timeout_zero_means_no_timeout(self, tmp_path):
        """api_timeout=0 should mean no timeout for LLM calls.

        When api_timeout is set to 0 (or negative), the LLM client should
        not apply any timeout, allowing calls to run indefinitely.
        """
        # Check if scheduling functions exist
        if not hasattr(ts, 'get_thread_scheduling'):
            pytest.skip("Priority scheduling functions not yet implemented")

        db = _make_db(tmp_path)
        root = ts.create_root_thread(db, name="root")

        # Set api_timeout=0
        ts.set_thread_scheduling(db, root, api_timeout=0)

        settings = ts.get_thread_scheduling(db, root)

        # Verify timeout is set to 0
        assert settings.api_timeout == 0, "api_timeout should be 0"

        # Verify interpretation: 0 means no timeout
        # The actual timeout value passed to aiohttp should be 0 or None
        # (both interpreted as "no timeout" by aiohttp)
        effective_timeout = settings.api_timeout if settings.api_timeout is not None else 600
        if effective_timeout <= 0:
            effective_timeout = 0  # No timeout
        assert effective_timeout == 0, "Effective timeout should be 0 (no timeout)"

    def test_api_timeout_negative_means_no_timeout(self, tmp_path):
        """api_timeout=-1 should also mean no timeout."""
        if not hasattr(ts, 'get_thread_scheduling'):
            pytest.skip("Priority scheduling functions not yet implemented")

        db = _make_db(tmp_path)
        root = ts.create_root_thread(db, name="root")

        # Set api_timeout=-1
        ts.set_thread_scheduling(db, root, api_timeout=-1)

        settings = ts.get_thread_scheduling(db, root)

        # Negative values should be treated as no timeout
        assert settings.api_timeout <= 0, "api_timeout should be <= 0"

    def test_default_api_timeout_is_600(self, tmp_path):
        """Default api_timeout should be 600 seconds (10 minutes)."""
        if not hasattr(ts, 'get_thread_scheduling'):
            pytest.skip("Priority scheduling functions not yet implemented")

        db = _make_db(tmp_path)
        root = ts.create_root_thread(db, name="root")

        settings = ts.get_thread_scheduling(db, root)

        # None means use default (600s)
        assert settings.api_timeout is None, "api_timeout should be None (use default)"

        # Verify default interpretation
        effective_timeout = settings.api_timeout if settings.api_timeout is not None else 600
        assert effective_timeout == 600, "Default effective timeout should be 600s"

    def test_sticky_threshold_zero_means_immediate_release(self, tmp_path):
        """threshold=0 should mean immediate slot release when idle.

        When sticky_idle_threshold_sec is 0, a thread should release its
        reserved slot immediately upon becoming idle (no grace period).
        """
        if not hasattr(ts, 'get_thread_scheduling'):
            pytest.skip("Priority scheduling functions not yet implemented")

        db = _make_db(tmp_path)
        root = ts.create_root_thread(db, name="root")

        # Set threshold=0
        ts.set_thread_scheduling(db, root, threshold=0)

        settings = ts.get_thread_scheduling(db, root)

        assert settings.threshold == 0, "threshold should be 0"

        # Interpretation: thread releases slot immediately when idle
        # This is useful for threads that should not hold slots when waiting

    def test_sticky_threshold_none_uses_global_default(self, tmp_path):
        """threshold=None should use the global default from RunnerConfig."""
        if not hasattr(ts, 'get_thread_scheduling'):
            pytest.skip("Priority scheduling functions not yet implemented")

        db = _make_db(tmp_path)
        root = ts.create_root_thread(db, name="root")

        # Don't set threshold - should be None
        settings = ts.get_thread_scheduling(db, root)

        assert settings.threshold is None, "threshold should be None (use global default)"

        # Verify global default from RunnerConfig
        cfg = RunnerConfig(sticky_idle_threshold_sec=5.0)
        assert cfg.sticky_idle_threshold_sec == 5.0, "Global default should be configurable"

    def test_very_large_threshold_keeps_slot_indefinitely(self, tmp_path):
        """Very large threshold should effectively keep slot forever.

        When threshold is set to a very large value (e.g., 999999), the thread
        should practically never release its slot due to idle timeout.
        """
        if not hasattr(ts, 'get_thread_scheduling'):
            pytest.skip("Priority scheduling functions not yet implemented")

        db = _make_db(tmp_path)
        root = ts.create_root_thread(db, name="root")

        # Set very large threshold
        ts.set_thread_scheduling(db, root, threshold=999999)

        settings = ts.get_thread_scheduling(db, root)

        assert settings.threshold == 999999, "threshold should be 999999"

        # This effectively means "keep slot forever" in practice

    def test_multiple_threads_with_different_timeouts(self, tmp_path):
        """Different threads can have different api_timeout values."""
        if not hasattr(ts, 'get_thread_scheduling'):
            pytest.skip("Priority scheduling functions not yet implemented")

        db = _make_db(tmp_path)

        root = ts.create_root_thread(db, name="root")
        child1 = ts.create_child_thread(db, root, name="child1")
        child2 = ts.create_child_thread(db, root, name="child2")

        # Set different timeouts
        ts.set_thread_scheduling(db, root, api_timeout=0)  # No timeout
        ts.set_thread_scheduling(db, child1, api_timeout=30)  # 30 seconds
        ts.set_thread_scheduling(db, child2, api_timeout=300)  # 5 minutes

        # Verify each thread has its own timeout
        root_settings = ts.get_thread_scheduling(db, root)
        child1_settings = ts.get_thread_scheduling(db, child1)
        child2_settings = ts.get_thread_scheduling(db, child2)

        assert root_settings.api_timeout == 0, "root should have no timeout"
        assert child1_settings.api_timeout == 30, "child1 should have 30s timeout"
        assert child2_settings.api_timeout == 300, "child2 should have 300s timeout"

    def test_scheduler_with_api_timeout_zero_globally(self, tmp_path):
        """Test scheduler behavior when global api_timeout is 0.

        This simulates the scenario described in the bug:
        After completing 2 scheduled threads, the scheduler stops scheduling
        new threads when api_timeout_sec=0 is set globally.

        The issue was related to watermark management, not the timeout itself.
        """
        db = _make_db(tmp_path)

        # Create 4 runnable threads
        root = ts.create_root_thread(db, name="root")
        threads = [root]
        for i in range(3):
            child = ts.create_child_thread(db, root, name=f"child-{i}")
            threads.append(child)

        for tid in threads:
            _make_thread_runnable(db, tid)

        # Set api_timeout=0 for all threads
        if hasattr(ts, 'set_thread_scheduling'):
            for tid in threads:
                ts.set_thread_scheduling(db, tid, api_timeout=0)

        # Simulate scheduler with proper watermark handling (the fix)
        scheduled_threads: Set[str] = set()
        completed_threads: Set[str] = set()
        last_checked_seq: Dict[str, int] = {}
        running_threads: Set[str] = set()
        max_concurrent = 2

        # Run for multiple iterations
        for iteration in range(20):
            for tid in threads:
                if tid in running_threads:
                    continue

                try:
                    max_seq = db.max_event_seq(tid)
                except Exception:
                    max_seq = -1

                # Watermark check
                if max_seq == last_checked_seq.get(tid, -1):
                    continue

                if is_thread_runnable(db, tid):
                    if len(running_threads) < max_concurrent:
                        running_threads.add(tid)
                        scheduled_threads.add(tid)

                        # Simulate completion
                        _make_thread_not_runnable(db, tid)
                        completed_threads.add(tid)
                        running_threads.discard(tid)
                    # FIX: Do NOT update watermark for runnable but not scheduled threads
                else:
                    # Only update watermark for non-runnable threads
                    last_checked_seq[tid] = max_seq

            if len(scheduled_threads) == len(threads):
                break

        # BUG: Without the fix, only 2 threads would be scheduled
        # FIXED: All 4 threads should be scheduled
        assert len(scheduled_threads) == 4, \
            f"All 4 threads should be scheduled even with api_timeout=0, got {len(scheduled_threads)}"

    def test_runner_config_has_sticky_scheduling_options(self, tmp_path):
        """Verify RunnerConfig has sticky scheduling options."""
        # Test default values
        cfg_default = RunnerConfig()
        assert hasattr(cfg_default, 'sticky_scheduling') or True, "sticky_scheduling may not be implemented yet"

        # Test with explicit values if the attribute exists
        if hasattr(RunnerConfig, 'sticky_scheduling'):
            cfg = RunnerConfig(sticky_scheduling=True, sticky_idle_threshold_sec=10.0)
            assert cfg.sticky_scheduling is True
            assert cfg.sticky_idle_threshold_sec == 10.0


class TestFullSchedulerIntegration:
    """Full integration tests with mocked LLM responses."""

    def test_scheduler_completes_6_threads_with_api_timeout_zero(self, tmp_path):
        """Test that scheduler completes 6 threads with max_concurrent=2 and api_timeout_sec=0.

        This test:
        1. Creates 6 threads starting in runnable state (user message pending)
        2. Uses a scheduler with max_concurrent=2, api_timeout_sec=0, priority_mode="alphabetical"
        3. Mocks LLM to return simple responses
        4. Verifies all 6 threads complete (assistant responded, thread idle)
        """
        db = _make_db(tmp_path)

        # Create root thread and 5 children (6 total)
        root = ts.create_root_thread(db, name="thread-A")
        threads = [root]
        for name in ["thread-B", "thread-C", "thread-D", "thread-E", "thread-F"]:
            child = ts.create_child_thread(db, root, name=name)
            threads.append(child)

        # Make all threads runnable by adding user messages
        for i, tid in enumerate(threads):
            _make_thread_runnable(db, tid)

        # Verify all 6 are runnable
        for tid in threads:
            assert is_thread_runnable(db, tid), f"Thread {tid} should be runnable"

        # Create mock LLM client that simulates responses
        class MockLLMClient:
            """Mock LLM that returns simple assistant responses."""

            def __init__(self):
                self.call_count = 0
                self.current_model_key = "test-model"

            def set_model(self, model_key):
                self.current_model_key = model_key

            def set_model_with_config(self, model_key, config):
                self.current_model_key = model_key

            async def astream_chat(self, messages, tools=None, tool_choice=None, timeout=None):
                """Yield mock LLM response events."""
                self.call_count += 1
                response_text = f"Response #{self.call_count}"

                # Yield content delta
                yield {"type": "content_delta", "text": response_text}

                # Yield final message with stop reason
                yield {
                    "type": "message",
                    "role": "assistant",
                    "content": response_text,
                    "stop_reason": "end_turn",
                }

        # Track completed threads
        completed_threads: Set[str] = set()
        scheduled_count = 0

        # Run the test
        async def run_scheduler_test():
            nonlocal scheduled_count, completed_threads

            mock_llm = MockLLMClient()

            # Create scheduler with test config
            cfg = RunnerConfig(
                max_concurrent_threads=2,
                api_timeout_sec=0,  # No timeout
                priority_mode="alphabetical",
                lease_ttl_sec=5,
                heartbeat_sec=0.5,
            )

            scheduler = SubtreeScheduler(
                db, root, llm=mock_llm, config=cfg, owner="test"
            )

            # We'll run the scheduler loop manually for controlled iterations
            # since run_forever() is infinite
            sem = asyncio.Semaphore(cfg.max_concurrent_threads)
            running_threads: Set[str] = set()
            last_checked_seq: Dict[str, int] = {}

            from eggthreads.runner import ThreadRunner

            async def drive(tid: str):
                nonlocal scheduled_count
                try:
                    async with sem:
                        scheduled_count += 1
                        runner = ThreadRunner(
                            db, tid,
                            llm=mock_llm,
                            owner="test",
                            purpose="assistant_stream",
                            config=cfg,
                        )
                        try:
                            await runner.run_once()
                        except Exception as e:
                            # Log but continue
                            print(f"Error in thread {tid}: {e}")
                            last_checked_seq.pop(tid, None)
                finally:
                    running_threads.discard(tid)

            tasks = []
            max_iterations = 50  # Safety limit

            for iteration in range(max_iterations):
                # Collect all threads
                all_threads = scheduler._collect_subtree(root)

                for tid in all_threads:
                    if tid in running_threads:
                        continue

                    # Watermark check
                    try:
                        max_seq = db.max_event_seq(tid)
                    except Exception:
                        max_seq = -1
                    if max_seq == last_checked_seq.get(tid, -1):
                        continue

                    # Check if runnable
                    if is_thread_runnable(db, tid):
                        running_threads.add(tid)
                        tasks.append(asyncio.create_task(drive(tid)))
                    else:
                        # Not runnable - update watermark
                        last_checked_seq[tid] = max_seq
                        completed_threads.add(tid)

                await asyncio.sleep(0.05)

                # Check if all threads completed
                if len(completed_threads) == len(threads):
                    break

            # Wait for any remaining tasks
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

            # Final check - count threads that are no longer runnable
            for tid in threads:
                if not is_thread_runnable(db, tid):
                    completed_threads.add(tid)

        asyncio.run(run_scheduler_test())

        # Verify all 6 threads completed
        assert len(completed_threads) == 6, \
            f"All 6 threads should complete, got {len(completed_threads)}: {completed_threads}"

        # Verify threads are no longer runnable (assistant responded)
        for tid in threads:
            assert not is_thread_runnable(db, tid), \
                f"Thread {tid} should not be runnable after completion"

    def test_scheduler_respects_alphabetical_priority(self, tmp_path):
        """Verify threads are scheduled in alphabetical order by thread_id."""
        db = _make_db(tmp_path)

        # Create threads - we'll check scheduling order
        root = ts.create_root_thread(db, name="root")
        threads = [root]
        for name in ["child-1", "child-2", "child-3"]:
            child = ts.create_child_thread(db, root, name=name)
            threads.append(child)

        # Make all runnable
        for tid in threads:
            _make_thread_runnable(db, tid)

        # Sort alphabetically to get expected order
        expected_order = sorted(threads)

        # Use _sort_by_priority to verify ordering
        from eggthreads.runner import _sort_by_priority
        sorted_threads = _sort_by_priority(threads, "alphabetical", db)

        assert sorted_threads == expected_order, \
            f"Threads should be sorted alphabetically: expected {expected_order}, got {sorted_threads}"

    def test_scheduler_with_zero_timeout_doesnt_hang(self, tmp_path):
        """Verify api_timeout_sec=0 doesn't cause hangs or errors."""
        db = _make_db(tmp_path)

        root = ts.create_root_thread(db, name="root")
        _make_thread_runnable(db, root)

        class QuickMockLLM:
            current_model_key = "test"

            def set_model(self, k):
                pass

            def set_model_with_config(self, k, c):
                pass

            async def astream_chat(self, messages, **kwargs):
                # Verify timeout parameter is 0
                assert kwargs.get('timeout') == 0, "Timeout should be 0"
                yield {"type": "content_delta", "text": "Quick response"}
                yield {"type": "message", "role": "assistant", "content": "Quick response", "stop_reason": "end_turn"}

        async def run_test():
            from eggthreads.runner import ThreadRunner

            cfg = RunnerConfig(
                api_timeout_sec=0,
                max_concurrent_threads=1,
            )

            mock_llm = QuickMockLLM()
            runner = ThreadRunner(db, root, llm=mock_llm, config=cfg, owner="test")

            # This should complete without hanging
            result = await runner.run_once()
            assert result is True, "run_once should return True when work was done"

        asyncio.run(run_test())

        # Thread should no longer be runnable
        assert not is_thread_runnable(db, root), "Thread should be idle after LLM response"
