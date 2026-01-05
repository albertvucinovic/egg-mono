"""Tests for commands/thread.py ThreadCommandsMixin."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

# Ensure project root is in path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class TestCmdNewThread:
    """Tests for cmd_newThread()."""

    def test_creates_root_thread(self, egg_app, monkeypatch):
        """Should create new root thread."""
        original_thread = egg_app.current_thread

        # Mock asyncio to avoid event loop issues
        monkeypatch.setattr(asyncio, "get_running_loop", lambda: type('Loop', (), {'create_task': lambda self, x: None})())

        egg_app.cmd_newThread("TestNewThread")

        assert egg_app.current_thread != original_thread

    def test_uses_default_name(self, egg_app, monkeypatch):
        """Should use 'Root' as default name."""
        # Mock asyncio to avoid event loop issues
        monkeypatch.setattr(asyncio, "get_running_loop", lambda: type('Loop', (), {'create_task': lambda self, x: None})())

        egg_app.cmd_newThread("")

        # Thread should have been created
        assert egg_app.current_thread is not None


class TestCmdSpawnChildThread:
    """Tests for cmd_spawnChildThread()."""

    def test_spawns_child_with_context(self, egg_app, monkeypatch):
        """Should spawn child thread with context text."""
        parent_thread = egg_app.current_thread

        # Mock tools.execute
        spawned = []
        class MockTools:
            def execute(self, name, args):
                spawned.append((name, args))
                return "child_thread_id_12345"

        monkeypatch.setattr(
            "eggthreads.tools.create_default_tools",
            lambda: MockTools()
        )

        egg_app.cmd_spawnChildThread("Do this task", text="/spawnChildThread Do this task")

        assert len(spawned) == 1
        assert spawned[0][0] == "spawn_agent"
        assert "Do this task" in spawned[0][1]["context_text"]

    def test_ensures_scheduler_for_child(self, egg_app, monkeypatch):
        """Should ensure scheduler for child thread."""
        ensured = []
        original_ensure = egg_app.ensure_scheduler_for
        def mock_ensure(tid):
            ensured.append(tid)
        monkeypatch.setattr(egg_app, "ensure_scheduler_for", mock_ensure)

        class MockTools:
            def execute(self, name, args):
                return "child_thread_id_12345"

        monkeypatch.setattr(
            "eggthreads.tools.create_default_tools",
            lambda: MockTools()
        )

        egg_app.cmd_spawnChildThread("Task")

        assert "child_thread_id_12345" in ensured


class TestCmdThread:
    """Tests for cmd_thread()."""

    def test_shows_current_thread_without_arg(self, egg_app):
        """Should display current thread when no arg."""
        egg_app.cmd_thread("")

        # Should log current thread info
        assert any(egg_app.current_thread[-8:] in msg for msg in egg_app._system_log)

    def test_switches_to_matching_thread(self, egg_app, monkeypatch):
        """Should switch to thread matching selector."""
        from eggthreads import create_root_thread, create_snapshot

        # Create another thread
        other_thread = create_root_thread(egg_app.db, name="OtherThread")
        create_snapshot(egg_app.db, other_thread)

        # Mock asyncio to avoid event loop issues
        monkeypatch.setattr(asyncio, "get_running_loop", lambda: type('Loop', (), {'create_task': lambda self, x: None})())

        egg_app.cmd_thread(other_thread[-8:])  # Use suffix

        assert egg_app.current_thread == other_thread

    def test_matches_by_name(self, egg_app, monkeypatch):
        """Should match by thread name."""
        from eggthreads import create_root_thread, create_snapshot

        named_thread = create_root_thread(egg_app.db, name="UniqueTestName")
        create_snapshot(egg_app.db, named_thread)

        # Mock asyncio to avoid event loop issues
        monkeypatch.setattr(asyncio, "get_running_loop", lambda: type('Loop', (), {'create_task': lambda self, x: None})())

        egg_app.cmd_thread("UniqueTestName")

        assert egg_app.current_thread == named_thread

    def test_logs_error_for_no_match(self, egg_app):
        """Should log error when no thread matches."""
        egg_app.cmd_thread("nonexistent_thread_xyz")

        assert any("No thread" in msg or "no thread" in msg.lower() for msg in egg_app._system_log)


class TestCmdDeleteThread:
    """Tests for cmd_deleteThread()."""

    def test_deletes_matching_thread(self, egg_app, monkeypatch):
        """Should delete thread matching selector."""
        from eggthreads import create_root_thread, create_snapshot, list_threads

        # Create thread to delete
        to_delete = create_root_thread(egg_app.db, name="ToDelete")
        create_snapshot(egg_app.db, to_delete)

        egg_app.cmd_deleteThread(to_delete[-8:])

        # Thread should be deleted
        threads = list_threads(egg_app.db)
        thread_ids = [t.thread_id for t in threads]
        assert to_delete not in thread_ids

    def test_prevents_deleting_current_thread(self, egg_app):
        """Should not delete current thread."""
        current = egg_app.current_thread

        egg_app.cmd_deleteThread(current[-8:])

        # Current thread should still exist
        assert egg_app.current_thread == current

    def test_requires_selector(self, egg_app):
        """Should show usage when no selector given."""
        egg_app.cmd_deleteThread("")

        assert any("Usage" in msg or "usage" in msg.lower() for msg in egg_app._system_log)


class TestCmdThreads:
    """Tests for cmd_threads()."""

    def test_lists_all_threads(self, egg_app):
        """Should list all threads."""
        egg_app.cmd_threads("")

        # Should log thread list
        assert any("Thread" in msg or "thread" in msg.lower() for msg in egg_app._system_log)


class TestCmdListChildren:
    """Tests for cmd_listChildren()."""

    def test_shows_no_subthreads_message(self, egg_app):
        """Should show message when no children."""
        egg_app.cmd_listChildren("")

        assert any("No subthread" in msg or "no subthread" in msg.lower() for msg in egg_app._system_log)

    def test_shows_children_tree(self, egg_app, monkeypatch):
        """Should show children tree when children exist."""
        from eggthreads import create_child_thread, create_snapshot

        child = create_child_thread(egg_app.db, egg_app.current_thread, name="ChildThread")
        create_snapshot(egg_app.db, child)

        egg_app.cmd_listChildren("")

        # Should log subtree info
        assert any("Subtree" in msg or "subtree" in msg.lower() or child[-8:] in msg for msg in egg_app._system_log)


class TestCmdParentThread:
    """Tests for cmd_parentThread()."""

    def test_moves_to_parent(self, egg_app, monkeypatch):
        """Should move to parent thread."""
        from eggthreads import create_child_thread, create_snapshot

        # Create child and switch to it
        child = create_child_thread(egg_app.db, egg_app.current_thread, name="ChildThread")
        create_snapshot(egg_app.db, child)
        parent = egg_app.current_thread
        egg_app.current_thread = child

        # Mock asyncio to avoid event loop issues
        monkeypatch.setattr(asyncio, "get_running_loop", lambda: type('Loop', (), {'create_task': lambda self, x: None})())

        egg_app.cmd_parentThread("")

        assert egg_app.current_thread == parent

    def test_shows_error_at_root(self, egg_app):
        """Should show error when already at root."""
        egg_app.cmd_parentThread("")

        assert any("root" in msg.lower() or "no parent" in msg.lower() for msg in egg_app._system_log)


class TestSelectThreadsBySelector:
    """Tests for select_threads_by_selector()."""

    def test_exact_id_match(self, egg_app):
        """Should match exact thread ID."""
        matches = egg_app.select_threads_by_selector(egg_app.current_thread)

        assert egg_app.current_thread in matches

    def test_suffix_match(self, egg_app):
        """Should match ID suffix."""
        suffix = egg_app.current_thread[-8:]
        matches = egg_app.select_threads_by_selector(suffix)

        assert egg_app.current_thread in matches

    def test_name_contains_match(self, egg_app):
        """Should match name containing selector."""
        matches = egg_app.select_threads_by_selector("Root")

        assert egg_app.current_thread in matches

    def test_returns_empty_for_no_match(self, egg_app):
        """Should return empty list for no match."""
        matches = egg_app.select_threads_by_selector("xyz_nonexistent_123")

        assert matches == []


class TestResolveSingleThreadSelector:
    """Tests for resolve_single_thread_selector()."""

    def test_returns_thread_id(self, egg_app):
        """Should return thread ID for valid selector."""
        result = egg_app.resolve_single_thread_selector(egg_app.current_thread[-8:])

        assert result == egg_app.current_thread

    def test_returns_none_for_no_match(self, egg_app):
        """Should return None for no match."""
        result = egg_app.resolve_single_thread_selector("xyz_nonexistent_123")

        assert result is None

    def test_returns_none_for_empty_selector(self, egg_app):
        """Should return None for empty selector."""
        result = egg_app.resolve_single_thread_selector("")

        assert result is None


class TestThreadRootId:
    """Tests for thread_root_id()."""

    def test_returns_root_for_root_thread(self, egg_app):
        """Should return same ID for root thread."""
        result = egg_app.thread_root_id(egg_app.current_thread)

        assert result == egg_app.current_thread

    def test_returns_root_for_child(self, egg_app):
        """Should return root ID for child thread."""
        from eggthreads import create_child_thread

        child = create_child_thread(egg_app.db, egg_app.current_thread, name="Child")

        result = egg_app.thread_root_id(child)

        assert result == egg_app.current_thread

    def test_returns_root_for_grandchild(self, egg_app):
        """Should return root ID for grandchild thread."""
        from eggthreads import create_child_thread

        child = create_child_thread(egg_app.db, egg_app.current_thread, name="Child")
        grandchild = create_child_thread(egg_app.db, child, name="Grandchild")

        result = egg_app.thread_root_id(grandchild)

        assert result == egg_app.current_thread


class TestIsThreadScheduled:
    """Tests for is_thread_scheduled()."""

    def test_returns_true_for_scheduled_root(self, egg_app):
        """Should return True for thread with active scheduler."""
        # Add scheduler entry
        egg_app.active_schedulers[egg_app.current_thread] = {"scheduler": None, "task": None}

        result = egg_app.is_thread_scheduled(egg_app.current_thread)

        assert result is True

    def test_returns_false_for_unscheduled(self, egg_app):
        """Should return False for thread without scheduler."""
        from eggthreads import create_root_thread

        other = create_root_thread(egg_app.db, name="Unscheduled")
        # Don't add to active_schedulers

        result = egg_app.is_thread_scheduled(other)

        assert result is False
