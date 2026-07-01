"""Provider streaming timeout semantics."""

from __future__ import annotations

import asyncio
import json
import sys

import pytest

from eggllm.providers.base import aiohttp_stream_timeout, requests_timeout_arg
from eggllm.providers.openai_compat import OpenAICompatAdapter


class _FakeAiohttpForHelper:
    class ClientTimeout:
        def __init__(self, **kwargs):
            self.kwargs = kwargs


def test_aiohttp_stream_timeout_is_inactivity_not_total_timeout():
    timeout = aiohttp_stream_timeout(_FakeAiohttpForHelper, 600)

    assert timeout.kwargs == {
        "total": None,
        "connect": 600.0,
        "sock_connect": 600.0,
        "sock_read": 600.0,
    }


def test_aiohttp_stream_timeout_zero_disables_timeout():
    timeout = aiohttp_stream_timeout(_FakeAiohttpForHelper, 0)

    assert timeout.kwargs == {"total": None}


def test_requests_timeout_zero_disables_timeout():
    assert requests_timeout_arg(0) is None
    assert requests_timeout_arg(-1) is None
    assert requests_timeout_arg(None) is None
    assert requests_timeout_arg(42) == 42.0


def test_openai_compat_async_uses_sock_read_inactivity_timeout(monkeypatch):
    timeout_kwargs = []

    class _FakeAiohttp:
        class ClientTimeout:
            def __init__(self, **kwargs):
                timeout_kwargs.append(kwargs)
                self.kwargs = kwargs

        class _Content:
            def __init__(self):
                self._lines = [
                    ("data: " + json.dumps({"choices": [{"delta": {"content": "ok"}}]})).encode("utf-8"),
                    b"data: [DONE]",
                ]

            async def readline(self):
                if self._lines:
                    return self._lines.pop(0)
                return b""

        class _Response:
            status = 200

            def __init__(self):
                self.content = _FakeAiohttp._Content()

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def text(self):
                return ""

        class ClientSession:
            def __init__(self, *args, **kwargs):
                self.kwargs = kwargs

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            def post(self, *args, **kwargs):
                return _FakeAiohttp._Response()

    monkeypatch.setitem(sys.modules, "aiohttp", _FakeAiohttp)

    async def collect():
        return [
            event
            async for event in OpenAICompatAdapter().stream_async(
                "https://example.test/v1/chat/completions",
                {},
                {"model": "gpt-test", "messages": [{"role": "user", "content": "Hi"}]},
                timeout=3,
            )
        ]

    out = asyncio.run(collect())

    assert out[0] == {"type": "content_delta", "text": "ok"}
    assert out[-1] == {"type": "done", "message": {"role": "assistant", "content": "ok"}}
    assert timeout_kwargs == [
        {"total": None, "connect": 3.0, "sock_connect": 3.0, "sock_read": 3.0}
    ]


async def _collect_openai_compat_from_handler(handler, *, timeout):
    pytest.importorskip("aiohttp")
    from aiohttp import web

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        assert site._server is not None
        sock = site._server.sockets[0]
        port = sock.getsockname()[1]
        return [
            event
            async for event in OpenAICompatAdapter().stream_async(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                {},
                {"model": "gpt-test", "messages": [{"role": "user", "content": "Hi"}]},
                timeout=timeout,
            )
        ]
    finally:
        await runner.cleanup()


def test_openai_compat_async_activity_can_exceed_timeout_wall_clock():
    pytest.importorskip("aiohttp")
    from aiohttp import web

    async def handler(request):
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        for text in ("a", "b", "c", "d"):
            payload = {"choices": [{"delta": {"content": text}}]}
            await resp.write(("data: " + json.dumps(payload) + "\n\n").encode("utf-8"))
            await asyncio.sleep(0.07)
        await resp.write(b"data: [DONE]\n\n")
        await resp.write_eof()
        return resp

    out = asyncio.run(_collect_openai_compat_from_handler(handler, timeout=0.2))

    assert [event for event in out if event["type"] == "content_delta"] == [
        {"type": "content_delta", "text": "a"},
        {"type": "content_delta", "text": "b"},
        {"type": "content_delta", "text": "c"},
        {"type": "content_delta", "text": "d"},
    ]
    assert out[-1] == {"type": "done", "message": {"role": "assistant", "content": "abcd"}}


def test_openai_compat_async_pre_stream_silence_times_out():
    pytest.importorskip("aiohttp")
    from aiohttp import web

    async def handler(request):
        await asyncio.sleep(0.5)
        return web.Response(text="data: [DONE]\n\n", headers={"Content-Type": "text/event-stream"})

    async def run():
        with pytest.raises(asyncio.TimeoutError):
            await _collect_openai_compat_from_handler(handler, timeout=0.2)

    asyncio.run(run())


def test_openai_compat_async_post_start_inactivity_times_out():
    pytest.importorskip("aiohttp")
    from aiohttp import web

    seen = []

    async def handler(request):
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        payload = {"choices": [{"delta": {"content": "first"}}]}
        await resp.write(("data: " + json.dumps(payload) + "\n\n").encode("utf-8"))
        await asyncio.sleep(0.5)
        await resp.write(b"data: [DONE]\n\n")
        return resp

    async def _run_with_server():
        app = web.Application()
        app.router.add_post("/v1/chat/completions", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        try:
            assert site._server is not None
            port = site._server.sockets[0].getsockname()[1]
            with pytest.raises(asyncio.TimeoutError):
                async for event in OpenAICompatAdapter().stream_async(
                    f"http://127.0.0.1:{port}/v1/chat/completions",
                    {},
                    {"model": "gpt-test", "messages": [{"role": "user", "content": "Hi"}]},
                    timeout=0.2,
                ):
                    seen.append(event)
        finally:
            await runner.cleanup()

    asyncio.run(_run_with_server())

    assert seen == [{"type": "content_delta", "text": "first"}]
