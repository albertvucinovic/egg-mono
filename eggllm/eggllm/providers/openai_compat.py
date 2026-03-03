import json
from typing import Dict, Any, Optional

import os
import requests

from .base import ProviderAdapter


class OpenAICompatAdapter(ProviderAdapter):
    """Streams OpenAI-compatible SSE responses from /chat/completions endpoints.

    Accumulates tool_calls per-index, stitching id/name/arguments across deltas.
    """

    def stream(self,
               url: str,
               headers: Dict[str, str],
               payload: Dict[str, Any],
               timeout: int = 600,
               session: Optional[requests.Session] = None):
        sess = session or requests
        resp = sess.post(url, headers=headers, json=payload, timeout=timeout, stream=True)
        resp.raise_for_status()

        assistant_text_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls_buf: Dict[int, Dict[str, Any]] = {}
        # Provider-specific message-level fields that we do not interpret
        # but must preserve (e.g. Gemini thought signatures).
        extra_message_fields: Dict[str, Any] = {}

        def tool_calls_values():
            # Preserve insertion order of indices
            return [tool_calls_buf[i] for i in sorted(tool_calls_buf.keys())]

        for line in resp.iter_lines():
            if not line:
                continue
            line_str = line.decode('utf-8', errors='ignore')
            if not line_str.startswith('data: '):
                continue
            data_str = line_str[6:]
            if data_str == '[DONE]':
                break
            try:
                payload = json.loads(data_str)
            except Exception:
                # Ignore malformed JSON chunks; the provider may send
                # non-JSON keep-alives or comments.
                continue

            # Some providers occasionally send streaming events with an
            # empty "choices" list. Our previous implementation assumed
            # at least one element (choices[0]) which caused an IndexError
            # ("list index out of range") and bubbled up as a runner
            # error. To make Egg robust against such deviations, we now
            # explicitly check that choices is a non-empty list before
            # attempting to read choices[0]. Events with no choices are
            # simply ignored.
            choices = payload.get("choices")
            if not isinstance(choices, list) or not choices:
                continue

            delta = (choices[0] or {}).get("delta", {})
            if not isinstance(delta, dict):
                continue

            # Preserve any provider-specific delta fields (other than the
            # standard OpenAI ones) so callers can round-trip them.
            for k, v in delta.items():
                if k in ("role", "content", "reasoning_content", "tool_calls"):
                    continue
                if v is None:
                    continue
                # Keep the latest non-empty value.
                extra_message_fields[k] = v

            if (content := delta.get("content")):
                assistant_text_parts.append(content)
                yield {"type": "content_delta", "text": content}

            if (reason := delta.get("reasoning_content")):
                reasoning_parts.append(reason)
                yield {"type": "reasoning_delta", "text": reason}

            if (tc_chunk := delta.get("tool_calls")):
                for tc_delta in tc_chunk:
                    raw_idx = tc_delta.get("index")
                    idx = raw_idx
                    if idx is None:
                        next_i = 0
                        while next_i in tool_calls_buf:
                            next_i += 1
                        idx = next_i
                    if idx not in tool_calls_buf:
                        # Defer to provider-sent id; do not invent our own to avoid mismatch on subsequent tool results
                        tool_calls_buf[idx] = {
                            "id": "",
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        }

                    # Preserve any provider-specific per-tool-call fields
                    # (e.g. Gemini 3 thought signatures under
                    # tool_call.extra_content).
                    for k, v in tc_delta.items():
                        if k in ("index", "id", "type", "function"):
                            continue
                        if v is None:
                            continue
                        # Do not merge: signatures must remain in their
                        # original position and cannot be combined.
                        # Overwrite with the latest value if the provider
                        # repeats the field across multiple chunks.
                        tool_calls_buf[idx][k] = v
                    if tc_delta.get("id"):
                        tool_calls_buf[idx]["id"] = tc_delta["id"]
                    if f_delta := tc_delta.get("function"):
                        if n := f_delta.get("name"):
                            tool_calls_buf[idx]["function"]["name"] += n
                        if a := f_delta.get("arguments"):
                            tool_calls_buf[idx]["function"]["arguments"] += a
                yield {"type": "tool_calls_delta", "delta": tool_calls_values()}

        final_message: Dict[str, Any] = {"role": "assistant"}
        content = "".join(assistant_text_parts)
        if content:
            final_message["content"] = content
        if tool_calls_buf:
            final_message["tool_calls"] = tool_calls_values()
        reasoning = "".join(reasoning_parts)
        if reasoning.strip():
            final_message["reasoning_content"] = reasoning

        # Attach any provider-specific message-level fields we observed.
        for k, v in extra_message_fields.items():
            if k not in final_message and v is not None:
                final_message[k] = v

        yield {"type": "done", "message": final_message}

    async def stream_async(self,
               url: str,
               headers: Dict[str, str],
               payload: Dict[str, Any],
               timeout: int = 600,
               session: Optional[Any] = None):
        """Async streaming using aiohttp.

        For Egg/eggthreads we rely on HTTP-level cancellation semantics
        (closing the underlying TCP connection when a stream is
        interrupted) so that local servers (e.g. llama.cpp) can stop
        generation promptly when the client disconnects.

        To guarantee this, aiohttp is a hard dependency here during
        normal operation. If it is not available and the environment
        variable ``EGG_FORCE_WITHOUT_AIOHTTP`` is **not** set, we raise a
        clear error. If that variable *is* set, we fall back to the
        thread-bridged implementation from ProviderAdapter (which does
        not guarantee hard HTTP cancellation).

        Additionally, if ``EGG_FORCE_WITHOUT_AIOHTTP`` is set, we will
        prefer the synchronous ``requests``-based adapter even when
        aiohttp is installed, so that advanced users can force the
        simpler code path for debugging or environments where aiohttp's
        behaviour is undesirable.
        """
        # If the user explicitly requested to run without aiohttp, prefer
        # the synchronous requests-based implementation even if aiohttp
        # is present.
        if os.environ.get("EGG_FORCE_WITHOUT_AIOHTTP"):
            # Run the synchronous stream() in a thread and forward events
            # into this async context.
            import asyncio
            loop = asyncio.get_running_loop()

            def _run_sync():
                return list(self.stream(url, headers, payload, timeout=timeout))

            events = await loop.run_in_executor(None, _run_sync)
            for evt in events:
                yield evt
            return
        try:
            import aiohttp
        except Exception as e:
            # aiohttp missing and no override flag -> require explicit
            # acknowledgement from the user via EGG_FORCE_WITHOUT_AIOHTTP.
            raise RuntimeError(
                "aiohttp is required for async streaming in eggllm. "
                "Install it (e.g. `pip install aiohttp`), or set "
                "EGG_FORCE_WITHOUT_AIOHTTP=1 to run without hard HTTP "
                "cancellation support."
            ) from e

        assistant_text_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls_buf: Dict[int, Dict[str, Any]] = {}
        extra_message_fields: Dict[str, Any] = {}

        def tool_calls_values():
            return [tool_calls_buf[i] for i in sorted(tool_calls_buf.keys())]

        client_timeout = aiohttp.ClientTimeout(total=timeout) if timeout else aiohttp.ClientTimeout(total=None)
        async with aiohttp.ClientSession(timeout=client_timeout) as sess:
            async with sess.post(url, headers=headers, json=payload) as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    raise RuntimeError(f"HTTP {resp.status}: {text}")
                # Iterate line by line from the SSE stream using readline() for proper delimitation
                while True:
                    line = await resp.content.readline()
                    if not line:
                        break
                    line_str = line.decode('utf-8', errors='ignore')
                    if not line_str.startswith('data: '):
                        continue
                    data_str = line_str[6:]
                    if data_str.strip() == '[DONE]':
                        break
                    try:
                        pj = json.loads(data_str)
                    except Exception:
                        # Ignore malformed JSON chunks; some servers send
                        # blank comments/keep-alives in the SSE channel.
                        continue

                    # See synchronous stream() above for rationale. We
                    # must guard against empty "choices" arrays to avoid
                    # IndexError when accessing choices[0].
                    choices = pj.get("choices")
                    if not isinstance(choices, list) or not choices:
                        continue

                    delta = (choices[0] or {}).get("delta", {})
                    if not isinstance(delta, dict):
                        continue

                    # Preserve any provider-specific delta fields.
                    for k, v in delta.items():
                        if k in ("role", "content", "reasoning_content", "tool_calls"):
                            continue
                        if v is None:
                            continue
                        extra_message_fields[k] = v
                    if (content := delta.get("content")):
                        assistant_text_parts.append(content)
                        yield {"type": "content_delta", "text": content}
                    if (reason := delta.get("reasoning_content")):
                        reasoning_parts.append(reason)
                        yield {"type": "reasoning_delta", "text": reason}
                    if (tc_chunk := delta.get("tool_calls")):
                        for tc_delta in tc_chunk:
                            raw_idx = tc_delta.get("index")
                            idx = raw_idx
                            if idx is None:
                                next_i = 0
                                while next_i in tool_calls_buf:
                                    next_i += 1
                                idx = next_i
                            if idx not in tool_calls_buf:
                                tool_calls_buf[idx] = {
                                    "id": "",
                                    "type": "function",
                                    "function": {"name": "", "arguments": ""},
                                }

                            # Preserve provider-specific per-tool-call fields.
                            for k, v in tc_delta.items():
                                if k in ("index", "id", "type", "function"):
                                    continue
                                if v is None:
                                    continue
                                tool_calls_buf[idx][k] = v
                            if tc_delta.get("id"):
                                tool_calls_buf[idx]["id"] = tc_delta["id"]
                            if f_delta := tc_delta.get("function"):
                                if n := f_delta.get("name"):
                                    tool_calls_buf[idx]["function"]["name"] += n
                                if a := f_delta.get("arguments"):
                                    tool_calls_buf[idx]["function"]["arguments"] += a
                        yield {"type": "tool_calls_delta", "delta": tool_calls_values()}

        final_message: Dict[str, Any] = {"role": "assistant"}
        content = "".join(assistant_text_parts)
        if content:
            final_message["content"] = content
        if tool_calls_buf:
            final_message["tool_calls"] = tool_calls_values()
        reasoning = "".join(reasoning_parts)
        if reasoning.strip():
            final_message["reasoning_content"] = reasoning

        for k, v in extra_message_fields.items():
            if k not in final_message and v is not None:
                final_message[k] = v

        yield {"type": "done", "message": final_message}

