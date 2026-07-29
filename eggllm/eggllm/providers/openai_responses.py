"""OpenAI Responses API adapter.

The Responses API is a newer OpenAI endpoint with different request/response
format compared to Chat Completions. Key differences:

- Endpoint: /v1/responses instead of /v1/chat/completions
- Input: Uses 'input' array + 'instructions' instead of 'messages'
- Streaming: Different SSE event types (response.content_part.delta, etc.)
- Built-in tools: web_search, code_interpreter, file_search
"""

import asyncio
from contextlib import asynccontextmanager
import json
import os
import random
import time
from typing import Dict, Any, Optional, List

import requests

from .base import (
    ProviderAdapter,
    aiohttp_stream_timeout,
    attach_provider_usage,
    requests_timeout_arg,
)


# Match Codex's built-in OpenAI provider defaults. These values count retries
# after the initial attempt, so requests can run 5 times and streams 6 times.
DEFAULT_REQUEST_MAX_RETRIES = 4
DEFAULT_STREAM_MAX_RETRIES = 5
_RETRY_BASE_DELAY_SECONDS = 0.2


class _RetryableStreamError(RuntimeError):
    """A stream failure that is safe to replay before output is emitted."""


class _HTTPResponseError(RuntimeError):
    """A non-retryable HTTP response, or one whose retry budget is exhausted."""


def _retry_delay_seconds(retry_number: int) -> float:
    """Codex-style exponential backoff with 10% jitter."""

    delay = _RETRY_BASE_DELAY_SECONDS * (2 ** max(0, retry_number - 1))
    return delay * random.uniform(0.9, 1.1)


def _sleep_sync(delay: float) -> None:
    time.sleep(delay)


async def _sleep_async(delay: float) -> None:
    await asyncio.sleep(delay)


def _close_sync_response(response: Any) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        close()


def _http_error_message(status: Any, body: Any) -> str:
    body_text = body if isinstance(body, str) else str(body or "")
    return f"HTTP {status}: {body_text}" if body_text else f"HTTP {status}"


def _post_sync_with_retries(
    session: Any,
    url: str,
    headers: Dict[str, str],
    payload: Dict[str, Any],
    timeout: int,
) -> Any:
    transport_errors = (
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
    )

    for request_retry in range(DEFAULT_REQUEST_MAX_RETRIES + 1):
        try:
            response = session.post(
                url,
                headers=headers,
                json=payload,
                timeout=requests_timeout_arg(timeout),
                stream=True,
            )
        except transport_errors as exc:
            if request_retry >= DEFAULT_REQUEST_MAX_RETRIES:
                raise RuntimeError(f"Responses API request failed: {exc}") from exc
            _sleep_sync(_retry_delay_seconds(request_retry + 1))
            continue

        status = getattr(response, "status_code", None)
        if isinstance(status, int) and status >= 400:
            error = _HTTPResponseError(
                _http_error_message(status, getattr(response, "text", ""))
            )
            _close_sync_response(response)
            if 500 <= status < 600 and request_retry < DEFAULT_REQUEST_MAX_RETRIES:
                _sleep_sync(_retry_delay_seconds(request_retry + 1))
                continue
            raise error

        # Preserve support for response-like test doubles without status_code.
        response.raise_for_status()
        return response

    raise AssertionError("unreachable")


def _iter_sync_stream_lines(response: Any):
    try:
        yield from response.iter_lines()
    except requests.exceptions.RequestException as exc:
        raise _RetryableStreamError(
            f"Responses API stream transport error: {exc}"
        ) from exc
    finally:
        _close_sync_response(response)


def _aiohttp_transport_errors(aiohttp: Any) -> tuple[type[BaseException], ...]:
    errors: tuple[type[BaseException], ...] = (TimeoutError,)
    client_error = getattr(aiohttp, "ClientError", None)
    if isinstance(client_error, type) and issubclass(client_error, BaseException):
        errors += (client_error,)
    return errors


@asynccontextmanager
async def _post_async_with_retries(
    session: Any,
    aiohttp: Any,
    url: str,
    headers: Dict[str, str],
    payload: Dict[str, Any],
):
    transport_errors = _aiohttp_transport_errors(aiohttp)

    for request_retry in range(DEFAULT_REQUEST_MAX_RETRIES + 1):
        response_context = None
        try:
            response_context = session.post(url, headers=headers, json=payload)
            response = await response_context.__aenter__()
        except transport_errors as exc:
            if request_retry >= DEFAULT_REQUEST_MAX_RETRIES:
                raise RuntimeError(f"Responses API request failed: {exc}") from exc
            await _sleep_async(_retry_delay_seconds(request_retry + 1))
            continue

        status = getattr(response, "status", 0)
        if isinstance(status, int) and status >= 400:
            try:
                body = await response.text()
            except transport_errors:
                body = ""
            await response_context.__aexit__(None, None, None)
            error = _HTTPResponseError(_http_error_message(status, body))
            if 500 <= status < 600 and request_retry < DEFAULT_REQUEST_MAX_RETRIES:
                await _sleep_async(_retry_delay_seconds(request_retry + 1))
                continue
            raise error

        try:
            yield response
        finally:
            await response_context.__aexit__(None, None, None)
        return

    raise AssertionError("unreachable")


async def _read_async_stream_line(content: Any, aiohttp: Any) -> bytes:
    try:
        return await content.readline()
    except _aiohttp_transport_errors(aiohttp) as exc:
        raise _RetryableStreamError(
            f"Responses API stream transport error: {exc}"
        ) from exc


def _responses_api_error(event_data: Dict[str, Any]) -> RuntimeError:
    response = event_data.get("response")
    response_error = response.get("error") if isinstance(response, dict) else None
    error_info = event_data.get("error")
    if not isinstance(error_info, dict):
        error_info = response_error if isinstance(response_error, dict) else {}

    code = error_info.get("code") or event_data.get("code") or "unknown"
    message = error_info.get("message") or event_data.get("message") or str(event_data)
    error_message = f"Responses API error ({code}): {message}"

    normalized_code = str(code).lower()
    normalized_message = str(message).lower()
    transient_codes = {
        "overloaded",
        "rate_limit_exceeded",
        "server_error",
        "service_unavailable",
        "timeout",
    }
    transient_phrases = (
        "error occurred while processing your request",
        "overload",
        "rate limit",
        "server error",
        "temporarily unavailable",
        "timed out",
        "timeout",
        "too many requests",
        "try again",
    )
    if normalized_code in transient_codes or any(
        phrase in normalized_message for phrase in transient_phrases
    ):
        return _RetryableStreamError(error_message)
    return RuntimeError(error_message)


class OpenAIResponsesAdapter(ProviderAdapter):
    """Streams OpenAI Responses API SSE responses.

    Converts Chat Completions message format to Responses API input format
    and parses the distinct SSE event stream back to normalized events.
    """

    def _convert_messages_to_input(self, messages: List[Dict[str, Any]]) -> tuple[Optional[str], List[Dict[str, Any]]]:
        """Convert Chat Completions messages to Responses API input format.

        Returns:
            (instructions, input_items) tuple where:
            - instructions: System message content (or None)
            - input_items: List of input items for the 'input' field
        """
        instructions: Optional[str] = None
        input_items: List[Dict[str, Any]] = []

        for msg in messages:
            role = msg.get("role")
            content = msg.get("content", "")

            if role == "system":
                # First system message becomes instructions
                if instructions is None:
                    if isinstance(content, str):
                        instructions = content
                    elif isinstance(content, list):
                        # Handle content arrays (e.g., with text parts)
                        text_parts = [
                            p.get("text", "")
                            for p in content
                            if isinstance(p, dict) and p.get("type") in ("text", "input_text")
                        ]
                        instructions = "\n".join(text_parts)
                    else:
                        instructions = str(content)
                continue

            if role == "user":
                item: Dict[str, Any] = {
                    "type": "message",
                    "role": "user",
                    "content": self._normalize_content(content)
                }
                input_items.append(item)

            elif role == "assistant":
                # Assistant messages with tool_calls need special handling
                tool_calls = msg.get("tool_calls") or []
                if tool_calls:
                    # Add function_call items for each tool call
                    # IMPORTANT: Responses API uses 'call_id' field, not 'id'!
                    for tc in tool_calls:
                        func = tc.get("function") or {}
                        # Chat Completions uses 'id', Responses API needs 'call_id'
                        tc_id = tc.get("id") or ""
                        if not tc_id:
                            # Skip tool calls without valid id
                            continue
                        fc_item: Dict[str, Any] = {
                            "type": "function_call",
                            "call_id": tc_id,  # Must be 'call_id' for Responses API!
                            "name": func.get("name") or "",
                            "arguments": func.get("arguments") or "{}",
                        }
                        input_items.append(fc_item)
                else:
                    # Regular assistant message
                    item = {
                        "type": "message",
                        "role": "assistant",
                        "content": self._normalize_content(content)
                    }
                    input_items.append(item)

            elif role == "tool":
                # Tool results become function_call_output
                # call_id is required by the Responses API - must be non-empty
                call_id = msg.get("tool_call_id") or ""
                if not call_id:
                    # Skip tool results without a valid call_id - this shouldn't
                    # happen in normal operation but prevents API errors
                    continue
                output_item: Dict[str, Any] = {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": content if isinstance(content, str) else json.dumps(content),
                }
                input_items.append(output_item)

        return instructions, input_items

    def _normalize_content(self, content: Any) -> Any:
        """Normalize content to a format the Responses API accepts."""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            # Pass through content arrays (may contain text, images, etc.)
            return content
        if content is None:
            return ""
        return str(content)

    def _convert_tools_to_responses_format(self, tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Convert Chat Completions tools format to Responses API format.

        Chat Completions format:
            {"type": "function", "function": {"name": "...", "description": "...", "parameters": {...}}}

        Responses API format:
            {"type": "function", "name": "...", "description": "...", "parameters": {...}}

        Built-in tools like web_search_preview are passed through unchanged.
        """
        converted = []
        for tool in tools:
            tool_type = tool.get("type", "function")

            # Built-in Responses API tools (web_search_preview, code_interpreter, etc.)
            # don't have a "function" nested object - pass through as-is
            if "function" not in tool:
                converted.append(tool)
                continue

            # Convert Chat Completions function format to Responses API format
            func = tool["function"]
            converted_tool: Dict[str, Any] = {
                "type": tool_type,
                "name": func.get("name", ""),
            }
            if "description" in func:
                converted_tool["description"] = func["description"]
            if "parameters" in func:
                converted_tool["parameters"] = func["parameters"]
            if "strict" in func:
                converted_tool["strict"] = func["strict"]

            converted.append(converted_tool)

        return converted

    def _build_payload(self, original_payload: Dict[str, Any]) -> Dict[str, Any]:
        """Build Responses API payload from Chat Completions format payload."""
        messages = original_payload.get("messages", [])
        instructions, input_items = self._convert_messages_to_input(messages)

        payload: Dict[str, Any] = {
            "model": original_payload.get("model"),
            "input": input_items,
            "stream": True,
        }

        # Always include instructions — the Codex backend (chatgpt.com)
        # requires this field for newer models like gpt-5.4.
        payload["instructions"] = instructions or ""

        # Convert and include tools if provided
        if "tools" in original_payload:
            payload["tools"] = self._convert_tools_to_responses_format(original_payload["tools"])

        # Pass through other common parameters
        for key in ("temperature", "top_p", "max_output_tokens", "max_tokens",
                     "reasoning", "store", "prompt_cache_key", "prompt_cache_retention"):
            if key in original_payload:
                # Responses API uses max_output_tokens, not max_tokens
                if key == "max_tokens":
                    payload["max_output_tokens"] = original_payload[key]
                else:
                    payload[key] = original_payload[key]

        # Convert flat reasoning_effort to nested reasoning object
        # (Responses API expects {"reasoning": {"effort": "high"}})
        if "reasoning_effort" in original_payload and "reasoning" not in payload:
            payload["reasoning"] = {"effort": original_payload["reasoning_effort"]}

        return payload

    def _response_incomplete_metadata(self, event_data: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize a Responses API response.incomplete event for persistence."""

        response = event_data.get("response") if isinstance(event_data.get("response"), dict) else {}
        details = event_data.get("incomplete_details")
        if details is None and isinstance(response, dict):
            details = response.get("incomplete_details")

        reason: Any = event_data.get("reason")
        if not reason and isinstance(details, dict):
            reason = details.get("reason")
        if not reason and isinstance(details, str):
            reason = details

        if not reason and isinstance(response, dict):
            reason = response.get("incomplete_reason") or response.get("status_details")

        if reason:
            if not isinstance(reason, str):
                try:
                    reason = json.dumps(reason, ensure_ascii=False, sort_keys=True)
                except Exception:
                    reason = str(reason)
            reason_text = f"response.incomplete: {reason}"
        else:
            reason_text = "response.incomplete"

        metadata: Dict[str, Any] = {
            "incomplete": True,
            "incomplete_reason": reason_text,
        }
        if details is not None:
            metadata["incomplete_details"] = details
        return metadata

    def stream(self,
               url: str,
               headers: Dict[str, str],
               payload: Dict[str, Any],
               timeout: int = 600,
               session: Optional[requests.Session] = None):
        for stream_retry in range(DEFAULT_STREAM_MAX_RETRIES + 1):
            emitted_event = False
            try:
                for event in self._stream_once(
                    url,
                    headers,
                    payload,
                    timeout=timeout,
                    session=session,
                ):
                    emitted_event = True
                    yield event
                return
            except _RetryableStreamError:
                # Replaying after deltas were emitted would duplicate persisted
                # text or tool-call arguments in Egg's thread runner.
                if emitted_event or stream_retry >= DEFAULT_STREAM_MAX_RETRIES:
                    raise
                _sleep_sync(_retry_delay_seconds(stream_retry + 1))

    def _stream_once(self,
                     url: str,
                     headers: Dict[str, str],
                     payload: Dict[str, Any],
                     timeout: int = 600,
                     session: Optional[requests.Session] = None):
        sess = session or requests
        api_payload = self._build_payload(payload)
        resp = _post_sync_with_retries(
            sess,
            url,
            headers,
            api_payload,
            timeout,
        )

        assistant_text_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls_buf: Dict[int, Dict[str, Any]] = {}
        current_output_index: int = -1
        incomplete_metadata: Dict[str, Any] = {}
        provider_usage: Optional[Dict[str, Any]] = None

        def tool_calls_values():
            return [tool_calls_buf[i] for i in sorted(tool_calls_buf.keys())]

        stream_finished = False
        for line in _iter_sync_stream_lines(resp):
            if not line:
                continue
            line_str = line.decode('utf-8', errors='ignore')
            if not line_str.startswith('data: '):
                continue
            data_str = line_str[6:]
            if data_str.strip() == '[DONE]':
                stream_finished = True
                break
            try:
                event_data = json.loads(data_str)
            except Exception:
                continue

            event_type = event_data.get("type", "")

            if event_type in ("response.completed", "response.done"):
                response = event_data.get("response") if isinstance(event_data.get("response"), dict) else {}
                usage = response.get("usage") if isinstance(response, dict) else None
                if not isinstance(usage, dict):
                    usage = event_data.get("usage")
                if isinstance(usage, dict):
                    provider_usage = usage

            # Handle different Responses API event types
            if event_type == "response.output_item.added":
                # New output item (could be message or function_call)
                item = event_data.get("item", {})
                current_output_index = event_data.get("output_index", current_output_index + 1)
                if item.get("type") == "function_call":
                    # IMPORTANT: Responses API has two IDs:
                    # - 'id': internal ID like "fc_xxx"
                    # - 'call_id': the ID used to match with function_call_output like "call_xxx"
                    # We MUST use 'call_id' for tool result matching to work!
                    call_id = item.get("call_id") or item.get("id") or ""
                    tool_calls_buf[current_output_index] = {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": item.get("name", ""),
                            "arguments": ""
                        }
                    }
                    yield {"type": "tool_calls_delta", "delta": tool_calls_values()}

            elif event_type == "response.content_part.delta":
                # Content delta (text or reasoning)
                delta = event_data.get("delta", {})
                part_type = delta.get("type", "")
                text = delta.get("text", "")

                if part_type == "text_delta" or part_type == "text":
                    if text:
                        assistant_text_parts.append(text)
                        yield {"type": "content_delta", "text": text}
                elif part_type == "reasoning" or "reasoning" in part_type.lower():
                    if text:
                        reasoning_parts.append(text)
                        yield {"type": "reasoning_delta", "text": text}

            elif event_type == "response.output_text.delta":
                # Alternative text delta event
                delta_text = event_data.get("delta", "")
                if delta_text:
                    assistant_text_parts.append(delta_text)
                    yield {"type": "content_delta", "text": delta_text}

            elif event_type == "response.function_call_arguments.delta":
                # Function call arguments streaming
                delta_args = event_data.get("delta", "")
                output_index = event_data.get("output_index", current_output_index)
                if output_index in tool_calls_buf and delta_args:
                    tool_calls_buf[output_index]["function"]["arguments"] += delta_args
                    yield {"type": "tool_calls_delta", "delta": tool_calls_values()}

            elif event_type == "response.function_call_arguments.done":
                # Function call complete - arguments finalized
                output_index = event_data.get("output_index", current_output_index)
                arguments = event_data.get("arguments", "")
                if output_index in tool_calls_buf:
                    # Use final arguments if provided
                    if arguments:
                        tool_calls_buf[output_index]["function"]["arguments"] = arguments
                    yield {"type": "tool_calls_delta", "delta": tool_calls_values()}

            elif event_type == "response.output_item.done":
                # Complete output item - contains final state with call_id
                item = event_data.get("item", {})
                output_index = event_data.get("output_index", current_output_index)
                if item.get("type") == "function_call":
                    # Update with final call_id and arguments from completed item
                    call_id = item.get("call_id") or item.get("id") or ""
                    if output_index in tool_calls_buf:
                        # Update existing entry with final values
                        if call_id:
                            tool_calls_buf[output_index]["id"] = call_id
                        if item.get("name"):
                            tool_calls_buf[output_index]["function"]["name"] = item["name"]
                        if item.get("arguments"):
                            tool_calls_buf[output_index]["function"]["arguments"] = item["arguments"]
                    else:
                        # Create new entry from completed item
                        tool_calls_buf[output_index] = {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": item.get("name", ""),
                                "arguments": item.get("arguments", "")
                            }
                        }
                    yield {"type": "tool_calls_delta", "delta": tool_calls_values()}

            elif event_type == "response.reasoning_summary_text.delta":
                # Reasoning summaries are display-only. Do not persist them as
                # reasoning_content for future provider requests.
                delta_text = event_data.get("delta", "")
                if delta_text:
                    yield {"type": "reasoning_summary_delta", "text": delta_text}

            elif event_type == "response.reasoning_text.delta":
                # Reasoning text delta (GPT-OSS models)
                delta_text = event_data.get("delta", "")
                if delta_text:
                    reasoning_parts.append(delta_text)
                    yield {"type": "reasoning_delta", "text": delta_text}

            elif event_type == "response.incomplete":
                # Generation stopped early (e.g. max_output_tokens reached).
                # Still yield whatever was accumulated — reasoning-only responses
                # are valid for reasoning models.
                incomplete_metadata = self._response_incomplete_metadata(event_data)
                stream_finished = True
                break

            elif event_type in ("response.failed", "error"):
                # Surface API/stream errors instead of silently ignoring them
                raise _responses_api_error(event_data)

            elif event_type in ("response.completed", "response.done"):
                # Stream complete
                stream_finished = True
                break

        if not stream_finished:
            raise _RetryableStreamError(
                "Responses API stream closed before response.completed"
            )

        # Build final message
        final_message: Dict[str, Any] = {"role": "assistant"}
        content = "".join(assistant_text_parts)
        if content:
            final_message["content"] = content
        if tool_calls_buf:
            final_message["tool_calls"] = tool_calls_values()
        reasoning = "".join(reasoning_parts)
        if reasoning.strip():
            final_message["reasoning_content"] = reasoning
        final_message.update(incomplete_metadata)
        attach_provider_usage(final_message, provider_usage)

        yield {"type": "done", "message": final_message}

    async def stream_async(self,
                           url: str,
                           headers: Dict[str, str],
                           payload: Dict[str, Any],
                           timeout: int = 600,
                           session: Optional[Any] = None):
        if os.environ.get("EGG_FORCE_WITHOUT_AIOHTTP"):
            async for event in self._stream_async_once(
                url,
                headers,
                payload,
                timeout=timeout,
                session=session,
            ):
                yield event
            return

        for stream_retry in range(DEFAULT_STREAM_MAX_RETRIES + 1):
            emitted_event = False
            try:
                async for event in self._stream_async_once(
                    url,
                    headers,
                    payload,
                    timeout=timeout,
                    session=session,
                ):
                    emitted_event = True
                    yield event
                return
            except _RetryableStreamError:
                if emitted_event or stream_retry >= DEFAULT_STREAM_MAX_RETRIES:
                    raise
                await _sleep_async(_retry_delay_seconds(stream_retry + 1))

    async def _stream_async_once(self,
                                 url: str,
                                 headers: Dict[str, str],
                                 payload: Dict[str, Any],
                                 timeout: int = 600,
                                 session: Optional[Any] = None):
        """Async streaming for Responses API.

        Similar to OpenAICompatAdapter, uses aiohttp for proper HTTP cancellation.
        Falls back to thread-bridged implementation if EGG_FORCE_WITHOUT_AIOHTTP is set.
        """
        if os.environ.get("EGG_FORCE_WITHOUT_AIOHTTP"):
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
            raise RuntimeError(
                "aiohttp is required for async streaming in eggllm. "
                "Install it (e.g. `pip install aiohttp`), or set "
                "EGG_FORCE_WITHOUT_AIOHTTP=1 to run without hard HTTP "
                "cancellation support."
            ) from e

        api_payload = self._build_payload(payload)

        assistant_text_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls_buf: Dict[int, Dict[str, Any]] = {}
        current_output_index: int = -1
        incomplete_metadata: Dict[str, Any] = {}
        provider_usage: Optional[Dict[str, Any]] = None

        def tool_calls_values():
            return [tool_calls_buf[i] for i in sorted(tool_calls_buf.keys())]

        client_timeout = aiohttp_stream_timeout(aiohttp, timeout)
        async with aiohttp.ClientSession(timeout=client_timeout) as sess:
            async with _post_async_with_retries(
                sess,
                aiohttp,
                url,
                headers,
                api_payload,
            ) as resp:
                stream_finished = False
                while True:
                    line = await _read_async_stream_line(resp.content, aiohttp)
                    if not line:
                        break
                    line_str = line.decode('utf-8', errors='ignore')
                    if not line_str.startswith('data: '):
                        continue
                    data_str = line_str[6:]
                    if data_str.strip() == '[DONE]':
                        stream_finished = True
                        break
                    try:
                        event_data = json.loads(data_str)
                    except Exception:
                        continue

                    event_type = event_data.get("type", "")

                    if event_type in ("response.completed", "response.done"):
                        response = event_data.get("response") if isinstance(event_data.get("response"), dict) else {}
                        usage = response.get("usage") if isinstance(response, dict) else None
                        if not isinstance(usage, dict):
                            usage = event_data.get("usage")
                        if isinstance(usage, dict):
                            provider_usage = usage

                    if event_type == "response.output_item.added":
                        item = event_data.get("item", {})
                        current_output_index = event_data.get("output_index", current_output_index + 1)
                        if item.get("type") == "function_call":
                            # IMPORTANT: Responses API has two IDs:
                            # - 'id': internal ID like "fc_xxx"
                            # - 'call_id': the ID used to match with function_call_output like "call_xxx"
                            # We MUST use 'call_id' for tool result matching to work!
                            call_id = item.get("call_id") or item.get("id") or ""
                            tool_calls_buf[current_output_index] = {
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": item.get("name", ""),
                                    "arguments": ""
                                }
                            }
                            yield {"type": "tool_calls_delta", "delta": tool_calls_values()}

                    elif event_type == "response.content_part.delta":
                        delta = event_data.get("delta", {})
                        part_type = delta.get("type", "")
                        text = delta.get("text", "")

                        if part_type == "text_delta" or part_type == "text":
                            if text:
                                assistant_text_parts.append(text)
                                yield {"type": "content_delta", "text": text}
                        elif part_type == "reasoning" or "reasoning" in part_type.lower():
                            if text:
                                reasoning_parts.append(text)
                                yield {"type": "reasoning_delta", "text": text}

                    elif event_type == "response.output_text.delta":
                        delta_text = event_data.get("delta", "")
                        if delta_text:
                            assistant_text_parts.append(delta_text)
                            yield {"type": "content_delta", "text": delta_text}

                    elif event_type == "response.function_call_arguments.delta":
                        delta_args = event_data.get("delta", "")
                        output_index = event_data.get("output_index", current_output_index)
                        if output_index in tool_calls_buf and delta_args:
                            tool_calls_buf[output_index]["function"]["arguments"] += delta_args
                            yield {"type": "tool_calls_delta", "delta": tool_calls_values()}

                    elif event_type == "response.function_call_arguments.done":
                        output_index = event_data.get("output_index", current_output_index)
                        arguments = event_data.get("arguments", "")
                        if output_index in tool_calls_buf:
                            if arguments:
                                tool_calls_buf[output_index]["function"]["arguments"] = arguments
                            yield {"type": "tool_calls_delta", "delta": tool_calls_values()}

                    elif event_type == "response.output_item.done":
                        # Complete output item - contains final state with call_id
                        item = event_data.get("item", {})
                        output_index = event_data.get("output_index", current_output_index)
                        if item.get("type") == "function_call":
                            # Update with final call_id and arguments from completed item
                            call_id = item.get("call_id") or item.get("id") or ""
                            if output_index in tool_calls_buf:
                                # Update existing entry with final values
                                if call_id:
                                    tool_calls_buf[output_index]["id"] = call_id
                                if item.get("name"):
                                    tool_calls_buf[output_index]["function"]["name"] = item["name"]
                                if item.get("arguments"):
                                    tool_calls_buf[output_index]["function"]["arguments"] = item["arguments"]
                            else:
                                # Create new entry from completed item
                                tool_calls_buf[output_index] = {
                                    "id": call_id,
                                    "type": "function",
                                    "function": {
                                        "name": item.get("name", ""),
                                        "arguments": item.get("arguments", "")
                                    }
                                }
                            yield {"type": "tool_calls_delta", "delta": tool_calls_values()}

                    elif event_type == "response.reasoning_summary_text.delta":
                        # Reasoning summaries are display-only. Do not persist
                        # them as reasoning_content for future provider requests.
                        delta_text = event_data.get("delta", "")
                        if delta_text:
                            yield {"type": "reasoning_summary_delta", "text": delta_text}

                    elif event_type == "response.reasoning_text.delta":
                        # Reasoning text delta (GPT-OSS models)
                        delta_text = event_data.get("delta", "")
                        if delta_text:
                            reasoning_parts.append(delta_text)
                            yield {"type": "reasoning_delta", "text": delta_text}

                    elif event_type == "response.incomplete":
                        # Generation stopped early (e.g. max_output_tokens reached).
                        # Still yield whatever was accumulated — reasoning-only responses
                        # are valid for reasoning models.
                        incomplete_metadata = self._response_incomplete_metadata(event_data)
                        stream_finished = True
                        break

                    elif event_type in ("response.failed", "error"):
                        # Surface API/stream errors instead of silently ignoring them
                        raise _responses_api_error(event_data)

                    elif event_type in ("response.completed", "response.done"):
                        stream_finished = True
                        break

                if not stream_finished:
                    raise _RetryableStreamError(
                        "Responses API stream closed before response.completed"
                    )

        final_message: Dict[str, Any] = {"role": "assistant"}
        content = "".join(assistant_text_parts)
        if content:
            final_message["content"] = content
        if tool_calls_buf:
            final_message["tool_calls"] = tool_calls_values()
        reasoning = "".join(reasoning_parts)
        if reasoning.strip():
            final_message["reasoning_content"] = reasoning
        final_message.update(incomplete_metadata)
        attach_provider_usage(final_message, provider_usage)

        yield {"type": "done", "message": final_message}
