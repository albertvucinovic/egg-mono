from __future__ import annotations

import asyncio
import json
import re
import uuid


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

    from egg.app import EggDisplayApp

    monkeypatch.setattr(EggDisplayApp, "start_scheduler", lambda self, root_tid: None)
    return EggDisplayApp()


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)


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

    # Patch EventWatcher so watch_thread runs the "preload" logic once and
    # then terminates (otherwise it polls forever).
    import egg.streaming as streaming_mod

    class _NoOpWatcher:
        def __init__(self, *args, **kwargs):
            pass

        async def aiter(self):
            if False:  # pragma: no cover - keep it an async generator
                yield []

    monkeypatch.setattr(streaming_mod, "EventWatcher", _NoOpWatcher)

    asyncio.run(app.watch_thread(tid))

    # 1) The chat panel should show that we are currently streaming.
    panel_text = app.compose_chat_panel_text()
    assert "Assistant (streaming)]" in panel_text
    assert "Hello ... world" in panel_text

    # 2) The thread list line should be labeled as STREAMING.
    thread_line = app.format_thread_line(tid)
    assert "STREAMING" in thread_line

    # Once the stream is closed and the lease released, the streaming markers
    # must disappear from the TUI.
    asyncio.run(
        app.ingest_event_for_live(
            {
                "type": "stream.close",
                "invoke_id": invoke_id,
                "payload_json": json.dumps({"finish_reason": "stop"}),
            },
            tid,
        )
    )
    app.db.release(tid, invoke_id)

    panel_text2 = app.compose_chat_panel_text()
    assert "Assistant (streaming)]" not in panel_text2
    thread_line2 = app.format_thread_line(tid)
    assert "STREAMING" not in thread_line2


def test_suppressed_tool_stream_shows_indicator_not_more_output(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    tid = app.current_thread
    invoke_id = _uid()

    asyncio.run(
        app.ingest_event_for_live(
            {
                "type": "stream.open",
                "invoke_id": invoke_id,
                "ts": "2024-01-01 00:00:00",
                "payload_json": json.dumps({"stream_kind": "tool"}),
            },
            tid,
        )
    )
    asyncio.run(
        app.ingest_event_for_live(
            {
                "type": "stream.delta",
                "invoke_id": invoke_id,
                "payload_json": json.dumps({"tool": {"name": "bash", "text": "preview"}}),
            },
            tid,
        )
    )
    asyncio.run(
        app.ingest_event_for_live(
            {
                "type": "stream.delta",
                "invoke_id": invoke_id,
                "payload_json": json.dumps({"tool": {"name": "bash", "suppressed": True}}),
            },
            tid,
        )
    )

    panel_text = app.compose_chat_panel_text()
    assert "preview" in panel_text
    assert "saving output only" in panel_text
    assert "tool bash: saving output" in app._current_stream_header_part()
    assert app._live_state["tool_stream_indicator"]["active"] is True


def test_tool_timeout_countdown_is_calculated_without_summary_events(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    tid = app.current_thread
    invoke_id = _uid()

    monkeypatch.setattr("egg.panels.time.time", lambda: 1030.0)

    asyncio.run(
        app.ingest_event_for_live(
            {
                "type": "stream.open",
                "invoke_id": invoke_id,
                "ts": "1970-01-01T00:16:40Z",
                "payload_json": json.dumps({"stream_kind": "tool"}),
            },
            tid,
        )
    )
    asyncio.run(
        app.ingest_event_for_live(
            {
                "type": "tool_call.execution_started",
                "payload_json": json.dumps({"tool_call_id": "call-wait", "timeout": 300}),
            },
            tid,
        )
    )

    assert app._live_state["timeout_sec"] == 300
    assert "timeout in 270s (limit 300s)" in app._current_stream_header_part()
    assert "timeout in 270s (limit 300s)" in app.compose_chat_panel_text()
    app.update_panels()
    assert "timeout in 270s (limit 300s)" in app.system_output.title

    asyncio.run(
        app.ingest_event_for_live(
            {
                "type": "msg.create",
                "payload_json": json.dumps({"role": "user", "content": "queued while tool is running"}),
            },
            tid,
        )
    )
    app.update_panels()
    assert "timeout in 270s (limit 300s)" in app.system_output.title


def test_tool_timeout_countdown_stays_in_header_with_tool_status(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    tid = app.current_thread
    invoke_id = _uid()

    monkeypatch.setattr("egg.panels.time.time", lambda: 1030.0)

    asyncio.run(
        app.ingest_event_for_live(
            {
                "type": "stream.open",
                "invoke_id": invoke_id,
                "ts": "1970-01-01T00:16:40Z",
                "payload_json": json.dumps({"stream_kind": "tool"}),
            },
            tid,
        )
    )
    asyncio.run(
        app.ingest_event_for_live(
            {
                "type": "tool_call.execution_started",
                "payload_json": json.dumps({"tool_call_id": "call-wait", "timeout_sec": 300}),
            },
            tid,
        )
    )
    asyncio.run(
        app.ingest_event_for_live(
            {
                "type": "tool_call.summary",
                "payload_json": json.dumps({"tool_call_id": "call-wait", "name": "wait", "summary": "waiting for child result"}),
            },
            tid,
        )
    )

    header = app._current_stream_header_part()
    assert "waiting for child result" in header
    assert "timeout in 270s (limit 300s)" in header


def test_tool_timeout_countdown_stays_in_header_with_suppressed_tool_stream(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    tid = app.current_thread
    invoke_id = _uid()

    monkeypatch.setattr("egg.panels.time.time", lambda: 1030.0)

    asyncio.run(
        app.ingest_event_for_live(
            {
                "type": "stream.open",
                "invoke_id": invoke_id,
                "ts": "1970-01-01T00:16:40Z",
                "payload_json": json.dumps({"stream_kind": "tool"}),
            },
            tid,
        )
    )
    asyncio.run(
        app.ingest_event_for_live(
            {
                "type": "tool_call.execution_started",
                "payload_json": json.dumps({"tool_call_id": "call-bash", "timeout_sec": 300}),
            },
            tid,
        )
    )
    asyncio.run(
        app.ingest_event_for_live(
            {
                "type": "stream.delta",
                "invoke_id": invoke_id,
                "payload_json": json.dumps({"tool": {"name": "bash", "suppressed": True}}),
            },
            tid,
        )
    )

    header = app._current_stream_header_part()
    assert "saving output" in header
    assert "timeout in 270s (limit 300s)" in header


def test_watch_thread_yields_while_replaying_large_active_reasoning_stream(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    tid = app.current_thread
    invoke_id = _uid()

    assert app.db.try_open_stream(
        thread_id=tid,
        invoke_id=invoke_id,
        lease_until_iso="9999-12-31T23:59:59Z",
        owner="pytest",
        purpose="llm",
    )
    app.db.append_event(
        event_id=_uid(),
        thread_id=tid,
        type_="stream.open",
        payload={"stream_kind": "llm"},
        msg_id=_uid(),
        invoke_id=invoke_id,
    )
    for i in range(250):
        app.db.append_event(
            event_id=_uid(),
            thread_id=tid,
            type_="stream.delta",
            payload={"reason": f"r{i}\n"},
            invoke_id=invoke_id,
            chunk_seq=i,
        )

    import egg.streaming as streaming_mod

    class _NoOpWatcher:
        def __init__(self, *args, **kwargs):
            pass

        async def aiter(self):
            if False:  # pragma: no cover - keep it an async generator
                yield []

    monkeypatch.setattr(streaming_mod, "EventWatcher", _NoOpWatcher)

    sleeps = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        sleeps.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(streaming_mod.asyncio, "sleep", fake_sleep)

    asyncio.run(app.watch_thread(tid))

    assert 0 in sleeps
    assert "r249" in app._live_state["reason"]


def test_stream_appends_are_coalesced_for_renderer(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    tid = app.current_thread
    invoke_id = _uid()

    calls = []

    class Renderer:
        def stream_begin(self):
            pass

        def stream_append(self, payload):
            calls.append(payload)

        def stream_end(self):
            pass

    app._renderer = Renderer()

    async def scenario():
        await app.ingest_event_for_live(
            {
                "type": "stream.open",
                "invoke_id": invoke_id,
                "ts": "2024-01-01 00:00:00",
                "payload_json": json.dumps({"stream_kind": "llm"}),
            },
            tid,
        )
        calls.clear()  # ignore header
        for i in range(10):
            await app.ingest_event_for_live(
                {
                    "type": "stream.delta",
                    "invoke_id": invoke_id,
                    "payload_json": json.dumps({"reason": str(i)}),
                },
                tid,
            )
        assert calls == []
        app._flush_stream_render_buffer_now()

    asyncio.run(scenario())

    assert len(calls) == 1
    assert all(str(i) in calls[0] for i in range(10))


def test_tool_output_stream_append_uses_plain_text_fast_path(tmp_path, monkeypatch):
    """Live tool stdout should not force Rich markup rendering per flush."""
    app = _make_app(tmp_path, monkeypatch)
    calls = []

    class Renderer:
        def stream_append(self, payload):
            calls.append(payload)

    app._renderer = Renderer()
    app._stream_append_on_renderer("literal tool output", style=None)
    app._flush_stream_render_buffer_now(force=True)

    assert calls == ["literal tool output"]


def test_bracketed_tool_output_streams_literally_in_fullscreen_renderer(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)

    from eggdisplay.renderers import FullScreenDiffRenderer

    class Renderer(FullScreenDiffRenderer):
        def _term_width(self):
            return 80

        def _term_height(self):
            return 5

        def _paint(self, width):
            self._prev_viewport = self._compose_visible_viewport(width)
            self._viewport_h = self._term_height()
            self._viewport_w = width

    renderer = Renderer()
    app._renderer = renderer
    renderer.stream_begin()
    app._stream_append_on_renderer("[literal tool output]\n", style=None)
    app._flush_stream_render_buffer_now(force=True)

    assert "[literal tool output]" in _strip_ansi(renderer._stream_buffer)
    assert any("[literal tool output]" in _strip_ansi(row) for row in renderer._prev_viewport)


def test_streaming_tool_call_arguments_show_tool_name_first(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    tid = app.current_thread
    invoke_id = _uid()
    calls = []

    class Renderer:
        def stream_begin(self):
            pass

        def stream_append(self, payload):
            calls.append(payload)

    app._renderer = Renderer()

    async def scenario():
        await app.ingest_event_for_live(
            {
                "type": "stream.open",
                "invoke_id": invoke_id,
                "ts": "2024-01-01 00:00:00",
                "payload_json": json.dumps({"stream_kind": "llm"}),
            },
            tid,
        )
        calls.clear()  # ignore stream header
        await app.ingest_event_for_live(
            {
                "type": "stream.delta",
                "invoke_id": invoke_id,
                "payload_json": json.dumps({
                    "tool_call": {
                        "id": "call_1",
                        "name": "bash",
                        "arguments_delta": '{"script":',
                    }
                }),
            },
            tid,
        )
        app._flush_stream_render_buffer_now(force=True)

    asyncio.run(scenario())

    rendered = "".join(calls)
    assert "Tool Call Args: bash" in rendered
    assert rendered.index("Tool Call Args: bash") < rendered.index('{"script":')
    assert "[Tool Call Args: bash]" in app.compose_chat_panel_text()


def test_streaming_tool_call_arguments_use_theme_style(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    app.apply_theme("cyberpunk")

    assert app._stream_payload_markup('args', style='egg.tool_call_dim').startswith('[egg.tool_call_dim]')

    tid = app.current_thread
    invoke_id = _uid()
    calls = []

    class Renderer:
        def stream_begin(self):
            pass

        def stream_append(self, payload):
            calls.append(payload)

    app._renderer = Renderer()

    async def scenario():
        await app.ingest_event_for_live(
            {
                "type": "stream.open",
                "invoke_id": invoke_id,
                "ts": "2024-01-01 00:00:00",
                "payload_json": json.dumps({"stream_kind": "llm"}),
            },
            tid,
        )
        calls.clear()
        await app.ingest_event_for_live(
            {
                "type": "stream.delta",
                "invoke_id": invoke_id,
                "payload_json": json.dumps({
                    "tool_call": {
                        "id": "call_1",
                        "name": "bash",
                        "arguments_delta": '{"script":',
                    }
                }),
            },
            tid,
        )
        app._flush_stream_render_buffer_now(force=True)

    asyncio.run(scenario())

    rendered = "".join(calls)
    assert "[egg.tool_call_dim]" in rendered
    assert "Tool Call Args: bash" in rendered


def test_stream_delta_only_batch_does_not_recompute_pending_prompt(tmp_path, monkeypatch):
    """Tool stream chunks should not rescan approval state for every batch."""
    app = _make_app(tmp_path, monkeypatch)
    tid = app.current_thread
    invoke_id = _uid()

    start_after = app.db.max_event_seq(tid)
    for i in range(25):
        app.db.append_event(
            event_id=_uid(),
            thread_id=tid,
            type_="stream.delta",
            payload={"tool": {"name": "bash", "suppressed": True}},
            invoke_id=invoke_id,
            chunk_seq=i,
        )
    batch = list(app.db.events_since(tid, start_after))

    import egg.streaming as streaming_mod

    class _OneBatchWatcher:
        def __init__(self, *args, **kwargs):
            pass

        async def aiter(self):
            yield batch

    monkeypatch.setattr(streaming_mod, "EventWatcher", _OneBatchWatcher)

    calls = {"count": 0}

    def counted_compute_pending_prompt():
        calls["count"] += 1

    monkeypatch.setattr(app, "compute_pending_prompt", counted_compute_pending_prompt)

    asyncio.run(app.watch_thread(tid))

    assert calls["count"] == 0


def test_approval_event_batch_recomputes_pending_prompt(tmp_path, monkeypatch):
    """Approval-related events still refresh the approval prompt."""
    app = _make_app(tmp_path, monkeypatch)
    tid = app.current_thread

    start_after = app.db.max_event_seq(tid)
    app.db.append_event(
        event_id=_uid(),
        thread_id=tid,
        type_="tool_call.approval",
        payload={"tool_call_id": "tc1", "decision": "granted"},
    )
    batch = list(app.db.events_since(tid, start_after))

    import egg.streaming as streaming_mod

    class _OneBatchWatcher:
        def __init__(self, *args, **kwargs):
            pass

        async def aiter(self):
            yield batch

    monkeypatch.setattr(streaming_mod, "EventWatcher", _OneBatchWatcher)

    calls = {"count": 0}

    monkeypatch.setattr(app, "compute_pending_prompt", lambda: calls.__setitem__("count", calls["count"] + 1))

    asyncio.run(app.watch_thread(tid))

    assert calls["count"] == 1


def test_watch_thread_yields_between_small_stream_batches(tmp_path, monkeypatch):
    """Continuous one-row stream batches should not monopolize the UI loop."""
    app = _make_app(tmp_path, monkeypatch)
    tid = app.current_thread
    invoke_id = _uid()
    batches = [
        [{
            "type": "stream.delta",
            "invoke_id": invoke_id,
            "payload_json": json.dumps({"tool": {"name": "bash", "suppressed": True}}),
        }]
        for _ in range(3)
    ]

    import egg.streaming as streaming_mod

    class _SmallBatchWatcher:
        def __init__(self, *args, **kwargs):
            pass

        async def aiter(self):
            for batch in batches:
                yield batch

    monkeypatch.setattr(streaming_mod, "EventWatcher", _SmallBatchWatcher)

    sleeps = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        sleeps.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(streaming_mod.asyncio, "sleep", fake_sleep)

    asyncio.run(app.watch_thread(tid))

    assert sleeps.count(0) >= len(batches)


def test_stream_flush_defers_renderer_while_input_is_dirty(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    calls = []

    class Renderer:
        def stream_append(self, payload):
            calls.append(payload)

    app._renderer = Renderer()
    app._stream_render_buffer = [("tool output", None)]
    app._stream_render_buffer_chars = len("tool output")
    app.input_panel.render()
    app.input_panel.editor.editor.insert_text("x")

    async def scenario():
        app._flush_stream_render_buffer_now()
        assert calls == []
        assert app._stream_render_buffer
        app.input_panel.render()
        app._flush_stream_render_buffer_now()

    asyncio.run(scenario())

    assert calls == ["tool output"]
    assert app._stream_render_buffer == []


def test_forced_stream_flush_ignores_dirty_input(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    calls = []

    class Renderer:
        def stream_append(self, payload):
            calls.append(payload)

    app._renderer = Renderer()
    app._stream_render_buffer = [("final chunk", None)]
    app._stream_render_buffer_chars = len("final chunk")
    app.input_panel.render()
    app.input_panel.editor.editor.insert_text("x")

    async def scenario():
        app._flush_stream_render_buffer_now(force=True)

    asyncio.run(scenario())

    assert calls == ["final chunk"]
    assert app._stream_render_buffer == []


def test_stream_close_then_final_message_prints_once_after_stream_end(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    tid = app.current_thread
    invoke_id = _uid()

    from eggthreads import append_message, create_snapshot

    append_message(app.db, tid, "user", "prompt before streaming")
    create_snapshot(app.db, tid)

    class Renderer:
        def __init__(self):
            self.sources = []
            self.events = []

        def set_scrollback_source(self, source):
            self.sources.append(source)

        def stream_begin(self):
            self.events.append("stream_begin")

        def stream_append(self, payload):
            self.events.append(("stream_append", payload))

        def stream_end(self):
            self.events.append("stream_end")

        def print_above(self, *args, **kwargs):
            self.events.append("print_above")

    renderer = Renderer()
    app._renderer = renderer
    app._display_is_inline = False
    assert app._install_transcript_scrollback_source(renderer) is True

    start_after = app._last_printed_seq_by_thread[tid]
    app.db.append_event(
        event_id=_uid(),
        thread_id=tid,
        type_="stream.open",
        payload={"stream_kind": "llm"},
        msg_id=_uid(),
        invoke_id=invoke_id,
    )
    app.db.append_event(
        event_id=_uid(),
        thread_id=tid,
        type_="stream.delta",
        payload={"text": "draft"},
        invoke_id=invoke_id,
        chunk_seq=0,
    )
    app.db.append_event(
        event_id=_uid(),
        thread_id=tid,
        type_="stream.close",
        payload={"finish_reason": "stop"},
        invoke_id=invoke_id,
    )
    final_msg_id = append_message(app.db, tid, "assistant", "final answer")

    batch = list(app.db.events_since(tid, start_after))

    import egg.streaming as streaming_mod

    class _OneBatchWatcher:
        def __init__(self, *args, **kwargs):
            pass

        async def aiter(self):
            yield batch

    monkeypatch.setattr(streaming_mod, "EventWatcher", _OneBatchWatcher)

    asyncio.run(app.watch_thread(tid))

    assert renderer.events.count("stream_end") == 1
    assert renderer.events.count("print_above") == 1
    assert renderer.events.index("stream_end") < renderer.events.index("print_above")
    assert len(renderer.sources) == 1

    final_seq = app.db.conn.execute(
        "SELECT event_seq FROM events WHERE msg_id=?",
        (final_msg_id,),
    ).fetchone()["event_seq"]
    assert app._last_printed_seq_by_thread[tid] == final_seq
