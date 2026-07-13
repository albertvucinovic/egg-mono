"""Focused responsiveness tests for Egg's input and completion dispatch."""
from __future__ import annotations

import asyncio
import threading

from egg.completion import AsyncCompletionWorker, CompletionRequest
from eggdisplay import TextEditor


def test_reader_notification_wakes_ui_and_input_dispatch_is_bounded(egg_app, monkeypatch):
    async def scenario() -> None:
        egg_app._ui_loop = asyncio.get_running_loop()
        egg_app._input_ready_event = asyncio.Event()
        seen = []
        monkeypatch.setattr(egg_app, "handle_key", lambda key: seen.append(key) or True)

        for key in "abcde":
            egg_app.input_panel.editor.input_queue.put(key)

        notifier = threading.Thread(target=egg_app._notify_input_ready)
        notifier.start()
        notifier.join()

        # A reader-thread callback, rather than the periodic UI timeout, makes
        # this wait complete. An outer timeout only prevents a broken test hang.
        await asyncio.wait_for(egg_app._wait_for_input_or_tick(10.0), timeout=1.0)
        had_input, keep_running = egg_app._drain_input_queue(limit=3)

        assert had_input is True
        assert keep_running is True
        assert seen == list("abc")
        assert egg_app.input_panel.editor.input_queue.qsize() == 2
        assert egg_app._input_ready_event.is_set()

    asyncio.run(scenario())


def test_async_completion_coalesces_pending_work_and_rejects_stale_result(
    isolated_db, monkeypatch
):
    started = threading.Event()
    release_first = threading.Event()
    worker_connection_ids = []
    calls = []

    def blocking_completion(line, col, db, get_current_thread, llm, command_registry):
        worker_connection_ids.append(id(db.conn))
        calls.append(line)
        if line == "a":
            started.set()
            assert release_first.wait(2.0)
        return [{"display": f"result-{line}", "insert": f"result-{line}"}]

    monkeypatch.setattr("egg.completion.get_autocomplete_items", blocking_completion)

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        finished = asyncio.Event()
        editor: TextEditor

        def apply(request, items):
            editor.apply_completion_result(
                request.generation,
                request.line,
                request.row,
                request.col,
                items,
            )
            if request.line == "abc":
                finished.set()

        worker = AsyncCompletionWorker(
            isolated_db.path,
            None,
            None,
            loop,
            apply,
        )

        def request(line, row, col, generation):
            worker.request(CompletionRequest(
                generation=generation,
                line=line,
                row=row,
                col=col,
                thread_id="thread-for-completion",
                snapshot_seq=7,
            ))
            return True

        editor = TextEditor(initial_text="a", async_autocomplete_callback=request)
        editor.cursor.col = 1
        try:
            assert editor.handle_key("tab") is True
            assert await asyncio.to_thread(started.wait, 1.0)

            # The first callback remains blocked in the worker, but typing and
            # scheduling a replacement request stay synchronous and immediate.
            assert editor.handle_key("b") is True
            assert editor.handle_key("c") is True
            assert editor.get_text() == "abc"
            current_generation = editor._completion_generation

            release_first.set()
            await asyncio.wait_for(finished.wait(), timeout=2.0)

            # Result "a" arrived first but could not overwrite the newer
            # text/cursor/generation identity. The queued "ab" request was
            # coalesced away, so only the latest request is shown.
            assert calls == ["a", "abc"]
            assert editor._completion_generation == current_generation
            assert editor._completion_items == [
                {"display": "result-abc", "insert": "result-abc"}
            ]
            assert editor.get_text() == "abc"
            assert worker_connection_ids
            assert all(connection_id != id(isolated_db.conn) for connection_id in worker_connection_ids)
        finally:
            worker.stop()
            await asyncio.to_thread(worker.join, 1.0)

    asyncio.run(scenario())


def test_async_completion_rejects_cursor_text_and_thread_snapshot_changes(egg_app):
    editor = egg_app.input_panel.editor.editor
    editor.set_text("alpha")
    editor.cursor.col = 5
    editor._completion_generation = 4
    editor._completion_pending = True
    editor._completion_request_mode = "tab"

    # Cursor identity is checked inside the editor.
    assert editor.apply_completion_result(
        4, "alpha", 0, 4, [{"display": "stale", "insert": "stale"}]
    ) is False
    assert editor._completion_items == []

    editor._completion_generation = 6
    editor._completion_pending = True
    assert editor.apply_completion_result(
        6, "old-alpha", 0, 5, [{"display": "stale-text", "insert": "stale-text"}]
    ) is False
    assert editor._completion_items == []

    # Thread and snapshot identity are checked by the application before it
    # delegates to the editor.
    editor._completion_generation = 8
    editor._completion_pending = True
    request = CompletionRequest(
        generation=8,
        line="alpha",
        row=0,
        col=5,
        thread_id="a-different-thread",
        snapshot_seq=-1,
    )
    egg_app._apply_async_completion(
        request, [{"display": "also-stale", "insert": "also-stale"}]
    )
    assert editor._completion_pending is False
    assert editor._completion_items == []


def test_async_completion_preserves_tab_navigation_and_acceptance():
    requests = []

    def request(line, row, col, generation):
        requests.append((line, row, col, generation))
        return True

    editor = TextEditor(initial_text="mo", async_autocomplete_callback=request)
    editor.cursor.col = 2

    assert editor.handle_key("tab") is True
    line, row, col, generation = requests[-1]
    assert editor.apply_completion_result(
        generation,
        line,
        row,
        col,
        [
            {"display": "model-one", "insert": "model-one", "replace": 2},
            {"display": "model-two", "insert": "model-two", "replace": 2},
        ],
    ) is True

    assert editor._completion_active is True
    assert editor.handle_key("down") is True
    assert editor.handle_key("enter") is True
    assert editor.get_text() == "model-two"
    assert editor._completion_active is False
    assert editor._completion_pending is False


def test_async_single_tab_completion_is_accepted_immediately():
    requests = []

    def request(line, row, col, generation):
        requests.append((line, row, col, generation))
        return True

    editor = TextEditor(initial_text="he", async_autocomplete_callback=request)
    editor.cursor.col = 2

    assert editor.handle_key("tab") is True
    line, row, col, generation = requests[-1]
    assert editor.apply_completion_result(
        generation,
        line,
        row,
        col,
        [{"display": "hello", "insert": "hello", "replace": 2}],
    ) is True

    assert editor.get_text() == "hello"
    assert editor._completion_active is False
