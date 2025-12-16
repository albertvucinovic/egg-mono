from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import uuid


# Pytest may chdir into a temporary directory; ensure we can still import the
# project module (egg.py).
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _uid() -> str:
    return uuid.uuid4().hex


def _make_app(tmp_path, monkeypatch):
    """Create an EggDisplayApp instance isolated to a temporary DB.

    We deliberately disable the background scheduler tasks because they
    run forever and are not needed to validate the TUI streaming
    rendering logic.
    """

    # ThreadsDB uses a relative path (".egg/threads.sqlite"), so isolate it.
    monkeypatch.chdir(tmp_path)

    # Avoid a hard dependency on aiohttp in test environments.
    # (Egg only needs aiohttp for HTTP cancellation during real streaming.)
    monkeypatch.setenv("EGG_FORCE_WITHOUT_AIOHTTP", "1")

    import egg

    monkeypatch.setattr(egg.EggDisplayApp, "_start_scheduler", lambda self, root_tid: None)
    return egg.EggDisplayApp()


def test_streaming_is_rendered_in_chat_panel_and_thread_list(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    tid = app.current_thread

    # Add at least one user message so the snapshot is non-trivial.
    from eggthreads import append_message, create_snapshot

    append_message(app.db, tid, "user", "Say hello slowly.")
    create_snapshot(app.db, tid)

    invoke_id = _uid()

    # Simulate an active stream lease so the thread list can mark it.
    ok = app.db.try_open_stream(
        thread_id=tid,
        invoke_id=invoke_id,
        lease_until_iso="9999-12-31T23:59:59Z",
        owner="pytest",
        purpose="assistant_stream",
    )
    assert ok is True

    # Write stream events to the DB (what a runner would do) so the watcher
    # preload path is exercised.
    app.db.append_event(
        event_id=_uid(),
        thread_id=tid,
        type_="stream.open",
        payload={},
        msg_id=_uid(),
        invoke_id=invoke_id,
    )
    app.db.append_event(
        event_id=_uid(),
        thread_id=tid,
        type_="stream.delta",
        payload={"text": "Hello"},
        invoke_id=invoke_id,
        chunk_seq=0,
    )
    app.db.append_event(
        event_id=_uid(),
        thread_id=tid,
        type_="stream.delta",
        payload={"text": " ... world"},
        invoke_id=invoke_id,
        chunk_seq=1,
    )

    # Patch EventWatcher so _watch_thread runs the "preload" logic once and
    # then terminates (otherwise it polls forever).
    import egg

    class _NoOpWatcher:
        def __init__(self, *args, **kwargs):
            pass

        async def aiter(self):
            if False:  # pragma: no cover - keep it an async generator
                yield []

    monkeypatch.setattr(egg, "EventWatcher", _NoOpWatcher)

    asyncio.run(app._watch_thread(tid))

    # 1) The chat panel should show that we are currently streaming.
    panel_text = app._compose_chat_panel_text()
    assert "Assistant (streaming)]" in panel_text
    assert "Hello ... world" in panel_text

    # 2) The thread list line should be labeled as STREAMING.
    thread_line = app._format_thread_line(tid)
    assert "STREAMING" in thread_line

    # Once the stream is closed and the lease released, the streaming markers
    # must disappear from the TUI.
    asyncio.run(
        app._ingest_event_for_live(
            {
                "type": "stream.close",
                "invoke_id": invoke_id,
                "payload_json": json.dumps({"finish_reason": "stop"}),
            },
            tid,
        )
    )
    app.db.release(tid, invoke_id)

    panel_text2 = app._compose_chat_panel_text()
    assert "Assistant (streaming)]" not in panel_text2
    thread_line2 = app._format_thread_line(tid)
    assert "STREAMING" not in thread_line2
