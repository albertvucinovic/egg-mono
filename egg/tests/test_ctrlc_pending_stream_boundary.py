from __future__ import annotations

"""Regression test for Ctrl+C cancel-before-first-delta behaviour.

Bug: If the user presses Ctrl+C after sending a user message but before
the runner emits any stream.open/stream.delta events, the system should
cancel the pending RA1 LLM turn. Previously, Egg only interrupted active
streams (open_streams leases). In the "pending" window, no lease existed,
so Ctrl+C did nothing and the scheduler would immediately start streaming
again.

Fix: eggthreads.api.interrupt_thread now appends a control.interrupt
boundary with purpose='llm' when it detects a pending RA1 turn with no
active lease.
"""

import uuid
import sys
from pathlib import Path


# Ensure we can import sibling libs (eggthreads lives next to the egg repo).
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SIBLING_ROOT = PROJECT_ROOT.parent
for p in (PROJECT_ROOT, SIBLING_ROOT / 'eggthreads'):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


def _uid() -> str:
    return uuid.uuid4().hex


def test_ctrlc_cancels_pending_ra1_turn(tmp_path, monkeypatch):
    # Isolate DB in tmp
    monkeypatch.chdir(tmp_path)

    from eggthreads import ThreadsDB, create_root_thread, append_message
    from eggthreads import create_snapshot, interrupt_thread
    from eggthreads.tool_state import discover_runner_actionable_cached

    db = ThreadsDB()
    db.init_schema()

    tid = create_root_thread(db, name="Root")
    append_message(db, tid, "system", "You are a helpful assistant.")
    append_message(db, tid, "user", "Hello")
    create_snapshot(db, tid)

    # Before Ctrl+C: there is a pending RA1 turn
    ra = discover_runner_actionable_cached(db, tid)
    assert ra is not None
    assert ra.kind == "RA1_llm"

    # Simulate Ctrl+C before any runner acquired a lease / emitted stream events
    interrupt_thread(db, tid, reason="pytest")

    # After Ctrl+C: RA1 should NOT be runnable again until a new user msg
    ra2 = discover_runner_actionable_cached(db, tid)
    assert ra2 is None
