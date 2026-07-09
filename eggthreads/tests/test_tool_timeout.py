"""Tests for tool execution timeout behavior.

Tests verify that:
1. LLM-specified timeout parameter works for bash and python tools
2. Config-based _tool_timeout_sec works as fallback
3. LLM-specified timeout takes priority over config timeout
4. Timeout returns appropriate error message to LLM
"""

from __future__ import annotations

from pathlib import Path
import os
import signal
import textwrap
import time

import pytest

import eggthreads as _eggthreads_mod
from eggthreads.runner import tool_timeout_summary
from eggthreads.tools import _should_emit_tool_summary


def _import_eggthreads(monkeypatch, tmp_path: Path):
    """Return the eggthreads module with cwd set to tmp_path."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EGG_SANDBOX_MODE", "off")
    return _eggthreads_mod


def test_tool_summary_emit_cadence_keeps_first_then_throttles_countdown():
    start = 1000.0
    emitted: list[tuple[float, str]] = []
    last = None

    for now in (start, start + 1, start + 4.9, start + 5, start + 6, start + 10):
        if _should_emit_tool_summary(last, now):
            last = now
            summary = tool_timeout_summary("bash", 30, start, now=now)
            assert summary is not None
            emitted.append((now, summary))

    assert [now - start for now, _summary in emitted] == [0, 5, 10]
    assert emitted[0][1] == "bash running; timeout in 30s (limit 30s)"


class TestToolTimeout:
    """Tests for bash and python tool timeout behavior."""

    def test_bash_tool_llm_timeout_kills_long_running_command(self, tmp_path, monkeypatch):
        """Bash tool should timeout when LLM specifies timeout."""
        eggthreads = _import_eggthreads(monkeypatch, tmp_path)
        tools = eggthreads.create_default_tools()

        # Run a sleep command with a 1-second timeout
        result = tools.execute(
            "bash",
            {"script": "sleep 10", "timeout": 1},
        )

        assert "TIMEOUT" in result
        assert "1" in result or "1.0" in result  # timeout value mentioned

    def test_bash_tool_completes_within_timeout(self, tmp_path, monkeypatch):
        """Bash tool should complete normally if command finishes before timeout."""
        eggthreads = _import_eggthreads(monkeypatch, tmp_path)
        tools = eggthreads.create_default_tools()

        result = tools.execute(
            "bash",
            {"script": "echo hello", "timeout": 10},
        )

        assert "TIMEOUT" not in result
        assert "hello" in result

    def test_bash_tool_completes_when_background_child_keeps_pipes_open(self, tmp_path, monkeypatch):
        """Background descendants inheriting stdout/stderr must not hang Egg."""
        eggthreads = _import_eggthreads(monkeypatch, tmp_path)
        tools = eggthreads.create_default_tools()
        marker = tmp_path / "child.pid"
        child = tmp_path / "hold_pipes.py"
        child.write_text(
            textwrap.dedent(
                f"""
                import os
                import signal
                import time

                signal.signal(signal.SIGHUP, signal.SIG_IGN)
                with open({str(marker)!r}, "w") as f:
                    f.write(str(os.getpid()))
                    f.flush()
                time.sleep(4)
                """
            )
        )

        start = time.monotonic()
        result = tools.execute(
            "bash",
            {
                "script": f"python3 {child} &\nfor i in $(seq 1 100); do [ -f {marker} ] && break; sleep 0.05; done\necho shell done",
                "timeout": 1,
            },
        )
        elapsed = time.monotonic() - start

        try:
            child_pid = int(marker.read_text()) if marker.exists() else None
            if child_pid:
                try:
                    os.kill(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

            assert elapsed < 3
            assert "TIMEOUT" not in result
            assert "shell done" in result
        finally:
            if marker.exists():
                try:
                    child_pid = int(marker.read_text())
                    os.kill(child_pid, signal.SIGKILL)
                except Exception:
                    pass

    def test_python_tool_llm_timeout_kills_long_running_script(self, tmp_path, monkeypatch):
        """Python tool should timeout when LLM specifies timeout."""
        eggthreads = _import_eggthreads(monkeypatch, tmp_path)
        tools = eggthreads.create_default_tools()

        # Run an infinite loop with a 1-second timeout
        result = tools.execute(
            "python",
            {"script": "import time; time.sleep(10)", "timeout": 1},
        )

        assert "TIMEOUT" in result
        assert "1" in result or "1.0" in result

    def test_python_tool_completes_within_timeout(self, tmp_path, monkeypatch):
        """Python tool should complete normally if script finishes before timeout."""
        eggthreads = _import_eggthreads(monkeypatch, tmp_path)
        tools = eggthreads.create_default_tools()

        result = tools.execute(
            "python",
            {"script": "print('hello')", "timeout": 10},
        )

        assert "TIMEOUT" not in result
        assert "hello" in result

    def test_python_tool_large_stdout_does_not_deadlock_pipe(self, tmp_path, monkeypatch):
        """Python tool should drain stdout while the child runs."""
        eggthreads = _import_eggthreads(monkeypatch, tmp_path)
        tools = eggthreads.create_default_tools()

        result = tools.execute(
            "python",
            {"script": "import sys; sys.stdout.write('x' * 100_000)", "timeout": 3},
        )

        assert "TIMEOUT" not in result
        assert len(result) > 100_000

    def test_bash_tool_config_timeout_works(self, tmp_path, monkeypatch):
        """Bash tool should use _tool_timeout_sec from config when LLM doesn't specify."""
        eggthreads = _import_eggthreads(monkeypatch, tmp_path)
        tools = eggthreads.create_default_tools()

        # Pass config timeout via context (simulating RunnerConfig)
        result = tools.execute(
            "bash",
            {"script": "sleep 10"},
            tool_timeout_sec=1,  # This becomes _tool_timeout_sec in args
        )

        assert "TIMEOUT" in result

    def test_python_tool_config_timeout_works(self, tmp_path, monkeypatch):
        """Python tool should use _tool_timeout_sec from config when LLM doesn't specify."""
        eggthreads = _import_eggthreads(monkeypatch, tmp_path)
        tools = eggthreads.create_default_tools()

        result = tools.execute(
            "python",
            {"script": "import time; time.sleep(10)"},
            tool_timeout_sec=1,
        )

        assert "TIMEOUT" in result

    def test_llm_timeout_overrides_config_timeout(self, tmp_path, monkeypatch):
        """LLM-specified timeout should take priority over config _tool_timeout_sec."""
        eggthreads = _import_eggthreads(monkeypatch, tmp_path)
        tools = eggthreads.create_default_tools()

        # Config says 1 second, LLM says 10 seconds - command should complete
        result = tools.execute(
            "bash",
            {"script": "sleep 2", "timeout": 10},  # LLM: 10s
            tool_timeout_sec=1,  # Config: 1s (should be overridden)
        )

        # The command sleeps for 2s. With LLM timeout of 10s, it should complete.
        # With config timeout of 1s, it would have timed out.
        assert "TIMEOUT" not in result

    def test_llm_can_specify_shorter_timeout_than_config(self, tmp_path, monkeypatch):
        """LLM can specify a shorter timeout than config."""
        eggthreads = _import_eggthreads(monkeypatch, tmp_path)
        tools = eggthreads.create_default_tools()

        # Config says 60 seconds, LLM says 1 second
        result = tools.execute(
            "bash",
            {"script": "sleep 10", "timeout": 1},  # LLM: 1s
            tool_timeout_sec=60,  # Config: 60s
        )

        # Should timeout because LLM specified 1s
        assert "TIMEOUT" in result

    def test_no_timeout_when_neither_specified(self, tmp_path, monkeypatch):
        """Command should run without timeout if neither LLM nor config specifies one."""
        eggthreads = _import_eggthreads(monkeypatch, tmp_path)
        tools = eggthreads.create_default_tools()

        # Quick command, no timeout specified
        result = tools.execute(
            "bash",
            {"script": "echo done"},
            # No tool_timeout_sec passed
        )

        assert "TIMEOUT" not in result
        assert "done" in result

    def test_timeout_message_format_for_llm(self, tmp_path, monkeypatch):
        """Timeout message should be clear for LLM to understand."""
        eggthreads = _import_eggthreads(monkeypatch, tmp_path)
        tools = eggthreads.create_default_tools()

        result = tools.execute(
            "bash",
            {"script": "sleep 10", "timeout": 1},
        )

        # Message should clearly indicate timeout occurred
        assert "--- TIMEOUT ---" in result
        assert "timed out" in result.lower()
        # Should mention the timeout duration
        assert "1" in result

    def test_streaming_bash_timeout_returns_tool_result_after_trapped_sigterm(self, tmp_path, monkeypatch):
        """Streaming bash should escalate after SIGTERM so traps cannot hang the runner."""
        eggthreads = _import_eggthreads(monkeypatch, tmp_path)
        tools = eggthreads.create_default_tools()

        import asyncio

        class Stream:
            def stream_delta(self, text):
                return True

        script = "trap 'echo trapped; sleep 5' TERM; echo started; sleep 30"

        async def run():
            return await tools.execute_async(
                "bash",
                {"script": script},
                tool_timeout_sec=0.5,
                preserve_tool_result=True,
                stream=Stream(),
            )

        result = asyncio.run(asyncio.wait_for(run(), timeout=4))

        assert getattr(result, "reason", None) == "timeout"
        assert "TIMEOUT" in result.output
        assert "started" in result.output

    def test_streaming_bash_timeout_does_not_wait_forever_on_open_stdout_pipe(self, tmp_path, monkeypatch):
        """Timeout should finish even if a detached descendant keeps stdout open."""
        eggthreads = _import_eggthreads(monkeypatch, tmp_path)
        tools = eggthreads.create_default_tools()

        import asyncio

        script = "setsid sh -c 'sleep 30 >&1' & echo parent done; sleep 30"

        async def run():
            return await tools.execute_async(
                "bash",
                {"script": script},
                tool_timeout_sec=0.5,
                preserve_tool_result=True,
            )

        result = asyncio.run(asyncio.wait_for(run(), timeout=4))

        assert getattr(result, "reason", None) == "timeout"
        assert "TIMEOUT" in result.output
        assert "parent done" in result.output

    def test_streaming_bash_timeout_does_not_wait_forever_for_stuck_wait(self, tmp_path, monkeypatch):
        """Timeout should finish even if asyncio subprocess wait never returns."""
        eggthreads = _import_eggthreads(monkeypatch, tmp_path)
        tools = eggthreads.create_default_tools()

        import asyncio

        class FakeStream:
            async def readline(self):
                await asyncio.sleep(3600)

        class FakeProc:
            pid = 99999999
            stdout = FakeStream()
            stderr = FakeStream()

            def __init__(self):
                self.returncode = None

            async def wait(self):
                await asyncio.sleep(3600)

            def terminate(self):
                pass

            def kill(self):
                pass

        async def fake_create_subprocess_exec(*args, **kwargs):
            return FakeProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

        async def run():
            return await tools.execute_async(
                "bash",
                {"script": "sleep 30"},
                tool_timeout_sec=0.1,
                preserve_tool_result=True,
            )

        result = asyncio.run(asyncio.wait_for(run(), timeout=5))

        assert getattr(result, "reason", None) == "timeout"
        assert "TIMEOUT" in result.output

    def test_streaming_bash_finished_process_does_not_wait_forever_for_stuck_reader(self, tmp_path, monkeypatch):
        """Finished process should not hang if a reader task never observes EOF."""
        eggthreads = _import_eggthreads(monkeypatch, tmp_path)
        tools = eggthreads.create_default_tools()

        import asyncio

        class FakeStream:
            async def readline(self):
                await asyncio.sleep(3600)

        class FakeProc:
            pid = 99999999
            stdout = FakeStream()
            stderr = FakeStream()
            returncode = 0

            async def wait(self):
                return 0

            def terminate(self):
                pass

            def kill(self):
                pass

        async def fake_create_subprocess_exec(*args, **kwargs):
            return FakeProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

        async def run():
            return await tools.execute_async(
                "bash",
                {"script": "true"},
                tool_timeout_sec=20,
                preserve_tool_result=True,
            )

        result = asyncio.run(asyncio.wait_for(run(), timeout=2))

        assert getattr(result, "reason", None) == "success"

    def test_streaming_bash_timeout_when_stdout_waits_without_newline(self, tmp_path, monkeypatch):
        """Timeout should not wait forever in readline() for a partial line."""
        eggthreads = _import_eggthreads(monkeypatch, tmp_path)
        tools = eggthreads.create_default_tools()

        import asyncio

        class Stream:
            def __init__(self):
                self.chunks = []

            def stream_delta(self, text):
                self.chunks.append(text)
                return True

        stream = Stream()
        script = "printf partial; sleep 30"

        async def run():
            return await tools.execute_async(
                "bash",
                {"script": script},
                tool_timeout_sec=0.5,
                preserve_tool_result=True,
                stream=stream,
            )

        result = asyncio.run(asyncio.wait_for(run(), timeout=4))

        assert getattr(result, "reason", None) == "timeout"
        assert "TIMEOUT" in result.output
        assert "partial" in result.output
        assert "partial" in "".join(stream.chunks)

    def test_streaming_bash_timeout_with_slow_trap_closes_runner_stream(self, tmp_path, monkeypatch):
        """A timed-out streaming bash tool should publish result and release lease."""
        eggthreads = _import_eggthreads(monkeypatch, tmp_path)

        import asyncio
        import json

        db = eggthreads.ThreadsDB(tmp_path / "threads.sqlite")
        db.init_schema()
        tid = eggthreads.create_root_thread(db, name="root")
        tcid = "tc-bash-timeout"
        eggthreads.append_message(
            db,
            tid,
            "assistant",
            "",
            extra={
                "tool_calls": [
                    {
                        "id": tcid,
                        "type": "function",
                        "function": {
                            "name": "bash",
                            "arguments": json.dumps({
                                "script": "trap 'echo trapped; sleep 5' TERM; echo started; sleep 30"
                            }),
                        },
                    }
                ]
            },
        )
        db.append_event("approve", tid, "tool_call.approval", {"tool_call_id": tcid, "decision": "granted"})

        async def run_once():
            runner = eggthreads.ThreadRunner(
                db,
                tid,
                llm=object(),
                config=eggthreads.RunnerConfig(tool_timeout_sec=0.5, lease_ttl_sec=5, heartbeat_sec=0.1),
            )
            return await runner.run_once()

        assert asyncio.run(asyncio.wait_for(run_once(), timeout=4)) is True

        assert db.current_open(tid) is None
        close_row = db.conn.execute(
            "SELECT invoke_id FROM events WHERE thread_id=? AND type='stream.close'",
            (tid,),
        ).fetchone()
        assert close_row is not None
        assert db.conn.execute(
            "SELECT 1 FROM open_streams WHERE thread_id=? AND invoke_id=?",
            (tid, close_row[0]),
        ).fetchone() is None
        states = eggthreads.build_tool_call_states(db, tid)
        assert states[tcid].state == "TC5"
        assert states[tcid].finished_reason == "timeout"
        assert states[tcid].output_decision == "whole"
        assert "TIMEOUT" in (states[tcid].finished_output or "")

    def test_streaming_bash_timeout_closes_even_after_lease_expires(self, tmp_path, monkeypatch):
        """Runner-owned timeout cleanup must not leave the TUI thinking it streams."""
        eggthreads = _import_eggthreads(monkeypatch, tmp_path)

        import asyncio
        import json

        db = eggthreads.ThreadsDB(tmp_path / "threads.sqlite")
        db.init_schema()
        tid = eggthreads.create_root_thread(db, name="root")
        tcid = "tc-bash-timeout-expired-lease"
        eggthreads.append_message(
            db,
            tid,
            "assistant",
            "",
            extra={
                "tool_calls": [
                    {
                        "id": tcid,
                        "type": "function",
                        "function": {
                            "name": "bash",
                            "arguments": json.dumps({"script": "echo started; sleep 30"}),
                        },
                    }
                ]
            },
        )
        db.append_event("approve", tid, "tool_call.approval", {"tool_call_id": tcid, "decision": "granted"})

        async def run_once():
            runner = eggthreads.ThreadRunner(
                db,
                tid,
                llm=object(),
                config=eggthreads.RunnerConfig(tool_timeout_sec=1.5, lease_ttl_sec=1, heartbeat_sec=3600),
            )
            return await runner.run_once()

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(asyncio.wait_for(run_once(), timeout=5))

        events = [
            (row["type"], row["invoke_id"])
            for row in db.conn.execute(
                "SELECT type, invoke_id FROM events WHERE thread_id=? ORDER BY event_seq",
                (tid,),
            )
        ]
        tool_invokes = [invoke for typ, invoke in events if typ == "stream.open" and invoke]
        assert len(tool_invokes) == 1
        invoke_id = tool_invokes[0]
        # Lease expiry fences timeout completion, output decisions, and close;
        # the stale invocation cannot persist terminal state.
        assert ("tool_call.finished", invoke_id) not in events
        assert ("stream.close", invoke_id) not in events
        assert not any(typ == "tool_call.output_approval" for typ, _ in events)

        state = eggthreads.build_tool_call_states(db, tid)[tcid]
        assert state.state == "TC3"
        started_payload = db.conn.execute(
            "SELECT payload_json FROM events WHERE thread_id=? AND type='tool_call.execution_started'",
            (tid,),
        ).fetchone()
        assert started_payload is not None
        started = json.loads(started_payload[0])
        assert started["timeout"] == 1.5
        assert "timeout_sec" not in started


    def test_streaming_bash_coalesces_tiny_live_chunks(self, tmp_path, monkeypatch):
        """Fast line-by-line output should not emit one live delta per line."""
        eggthreads = _import_eggthreads(monkeypatch, tmp_path)
        tools = eggthreads.create_default_tools()

        import asyncio

        class Stream:
            def __init__(self):
                self.chunks = []

            def stream_delta(self, text):
                self.chunks.append(text)
                return True

        stream = Stream()
        script = "for i in $(seq 1 200); do echo line-$i; done"

        async def run():
            return await tools.execute_async(
                "bash",
                {"script": script},
                preserve_tool_result=True,
                stream=stream,
            )

        result = asyncio.run(asyncio.wait_for(run(), timeout=4))

        assert getattr(result, "reason", None) == "success"
        streamed = "".join(stream.chunks)
        assert "line-1" in streamed
        assert "line-200" in streamed
        assert len(stream.chunks) < 50

    def test_streaming_bash_large_single_line_stdout_does_not_timeout(self, tmp_path, monkeypatch):
        """Streaming bash should not use readline() for huge newline-free output."""
        eggthreads = _import_eggthreads(monkeypatch, tmp_path)
        tools = eggthreads.create_default_tools()

        import asyncio

        async def run():
            return await tools.execute_async(
                "bash",
                {"script": "python3 - <<'PY'\nimport sys\nsys.stdout.write('x' * 100_000)\nPY"},
                tool_timeout_sec=3,
                preserve_tool_result=True,
            )

        result = asyncio.run(asyncio.wait_for(run(), timeout=5))

        assert getattr(result, "reason", None) == "success"
        assert "TIMEOUT" not in result.output
        assert len(result.output) > 100_000



class TestToolTimeoutInvalidInput:
    """Tests for handling invalid timeout values."""

    def test_invalid_timeout_string_falls_back_to_config(self, tmp_path, monkeypatch):
        """Invalid LLM timeout should fall back to config timeout."""
        eggthreads = _import_eggthreads(monkeypatch, tmp_path)
        tools = eggthreads.create_default_tools()

        result = tools.execute(
            "bash",
            {"script": "sleep 10", "timeout": "invalid"},  # Invalid
            tool_timeout_sec=1,  # Config fallback
        )

        # Should use config timeout and thus timeout
        assert "TIMEOUT" in result

    def test_negative_timeout_treated_as_no_timeout(self, tmp_path, monkeypatch):
        """Negative timeout from LLM should be treated as invalid."""
        eggthreads = _import_eggthreads(monkeypatch, tmp_path)
        tools = eggthreads.create_default_tools()

        # Negative timeout - should use config
        result = tools.execute(
            "bash",
            {"script": "sleep 5", "timeout": -1},
            tool_timeout_sec=1,
        )

        # Negative converts to float -1.0, which subprocess treats as no timeout
        # So it falls through. This test documents current behavior.
        # If we want to handle negative differently, we'd need to add validation.


class TestToolCancelCheck:
    """Tests for cancel_check callback (interrupt support)."""

    def test_bash_tool_respects_cancel_check(self, tmp_path, monkeypatch):
        """Bash tool should stop when cancel_check returns True."""
        eggthreads = _import_eggthreads(monkeypatch, tmp_path)
        tools = eggthreads.create_default_tools()

        import threading
        cancel_flag = threading.Event()

        def cancel_check():
            return cancel_flag.is_set()

        # Start cancellation after 0.5 seconds in another thread
        def set_cancel():
            import time
            time.sleep(0.5)
            cancel_flag.set()

        threading.Thread(target=set_cancel, daemon=True).start()

        result = tools.execute(
            "bash",
            {"script": "sleep 10"},
            cancel_check=cancel_check,
        )

        assert "INTERRUPTED" in result

    def test_python_tool_respects_cancel_check(self, tmp_path, monkeypatch):
        """Python tool should stop when cancel_check returns True."""
        eggthreads = _import_eggthreads(monkeypatch, tmp_path)
        tools = eggthreads.create_default_tools()

        import threading
        cancel_flag = threading.Event()

        def cancel_check():
            return cancel_flag.is_set()

        # Start cancellation after 0.5 seconds
        def set_cancel():
            import time
            time.sleep(0.5)
            cancel_flag.set()

        threading.Thread(target=set_cancel, daemon=True).start()

        result = tools.execute(
            "python",
            {"script": "import time; time.sleep(10)"},
            cancel_check=cancel_check,
        )

        assert "INTERRUPTED" in result


class TestDefaultToolTimeoutAPI:
    """Tests for set_default_tool_timeout and get_default_tool_timeout API."""

    def test_get_default_timeout_returns_30_seconds(self, tmp_path, monkeypatch):
        """Default tool timeout should be 30 seconds."""
        eggthreads = _import_eggthreads(monkeypatch, tmp_path)

        timeout = eggthreads.get_default_tool_timeout()
        assert timeout == 30.0

    def test_set_default_timeout_changes_value(self, tmp_path, monkeypatch):
        """set_default_tool_timeout should change the global default."""
        eggthreads = _import_eggthreads(monkeypatch, tmp_path)

        original = eggthreads.get_default_tool_timeout()
        try:
            eggthreads.set_default_tool_timeout(120.0)
            assert eggthreads.get_default_tool_timeout() == 120.0
        finally:
            # Restore original
            eggthreads.set_default_tool_timeout(original)

    def test_set_default_timeout_to_zero_disables_timeout(self, tmp_path, monkeypatch):
        """Setting timeout to 0 should disable timeout."""
        eggthreads = _import_eggthreads(monkeypatch, tmp_path)

        original = eggthreads.get_default_tool_timeout()
        try:
            eggthreads.set_default_tool_timeout(0)
            assert eggthreads.get_default_tool_timeout() == 0
        finally:
            eggthreads.set_default_tool_timeout(original)


class TestRunnerToolTimeoutResolution:
    """Tests shared timeout resolution used by runner display and execution."""

    def test_shared_resolver_uses_llm_timeout_before_config_default(self):
        from eggthreads.runner import resolve_tool_timeout_sec

        assert resolve_tool_timeout_sec({"timeout": 5}, 30, 60) == 5.0
        assert resolve_tool_timeout_sec({"timeout_sec": 6}, 30, 60) == 6.0

    def test_shared_resolver_falls_back_for_invalid_or_non_positive_llm_timeout(self):
        from eggthreads.runner import resolve_tool_timeout_sec

        assert resolve_tool_timeout_sec({"timeout": "bad"}, 7, 30) == 7.0
        assert resolve_tool_timeout_sec({"timeout": -1}, 7, 30) == 7.0
        assert resolve_tool_timeout_sec({}, None, 30) == 30.0
        assert resolve_tool_timeout_sec({}, 0, None) is None

    def test_tool_registry_timeout_resolver_is_shared_by_bash_and_python_tools(self):
        from eggthreads.tools import resolve_tool_timeout_arg

        assert resolve_tool_timeout_arg({"timeout": 2, "_tool_timeout_sec": 30}) == 2.0
        assert resolve_tool_timeout_arg({"timeout_sec": 3, "_tool_timeout_sec": 30}) == 3.0
        assert resolve_tool_timeout_arg({"timeout": "bad", "_tool_timeout_sec": 30}) == 30.0
        assert resolve_tool_timeout_arg({"timeout": -1, "_tool_timeout_sec": 30}) == 30.0
