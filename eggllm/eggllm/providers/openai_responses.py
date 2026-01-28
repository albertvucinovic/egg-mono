"""OpenAI Responses API adapter.

The Responses API is a newer OpenAI endpoint with different request/response
format compared to Chat Completions. Key differences:

- Endpoint: /v1/responses instead of /v1/chat/completions
- Input: Uses 'input' array + 'instructions' instead of 'messages'
- Streaming: Different SSE event types (response.content_part.delta, etc.)
- Built-in tools: web_search, code_interpreter, file_search
"""

import json
import os
from typing import Dict, Any, Optional, List

import requests

from .base import ProviderAdapter


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
                        text_parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
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

        if instructions:
            payload["instructions"] = instructions

        # Convert and include tools if provided
        if "tools" in original_payload:
            payload["tools"] = self._convert_tools_to_responses_format(original_payload["tools"])

        # Pass through other common parameters
        for key in ("temperature", "top_p", "max_output_tokens", "max_tokens", "reasoning"):
            if key in original_payload:
                # Responses API uses max_output_tokens, not max_tokens
                if key == "max_tokens":
                    payload["max_output_tokens"] = original_payload[key]
                else:
                    payload[key] = original_payload[key]

        return payload

    def stream(self,
               url: str,
               headers: Dict[str, str],
               payload: Dict[str, Any],
               timeout: int = 600,
               session: Optional[requests.Session] = None):
        sess = session or requests
        api_payload = self._build_payload(payload)
        resp = sess.post(url, headers=headers, json=api_payload, timeout=timeout, stream=True)
        resp.raise_for_status()

        assistant_text_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls_buf: Dict[int, Dict[str, Any]] = {}
        current_output_index: int = -1

        def tool_calls_values():
            return [tool_calls_buf[i] for i in sorted(tool_calls_buf.keys())]

        for line in resp.iter_lines():
            if not line:
                continue
            line_str = line.decode('utf-8', errors='ignore')
            if not line_str.startswith('data: '):
                continue
            data_str = line_str[6:]
            if data_str.strip() == '[DONE]':
                break
            try:
                event_data = json.loads(data_str)
            except Exception:
                continue

            event_type = event_data.get("type", "")

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

            elif event_type in ("response.completed", "response.done"):
                # Stream complete
                break

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

        yield {"type": "done", "message": final_message}

    async def stream_async(self,
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

        def tool_calls_values():
            return [tool_calls_buf[i] for i in sorted(tool_calls_buf.keys())]

        client_timeout = aiohttp.ClientTimeout(total=timeout) if timeout else aiohttp.ClientTimeout(total=None)
        async with aiohttp.ClientSession(timeout=client_timeout) as sess:
            async with sess.post(url, headers=headers, json=api_payload) as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    raise RuntimeError(f"HTTP {resp.status}: {text}")

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
                        event_data = json.loads(data_str)
                    except Exception:
                        continue

                    event_type = event_data.get("type", "")

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

                    elif event_type in ("response.completed", "response.done"):
                        break

        final_message: Dict[str, Any] = {"role": "assistant"}
        content = "".join(assistant_text_parts)
        if content:
            final_message["content"] = content
        if tool_calls_buf:
            final_message["tool_calls"] = tool_calls_values()
        reasoning = "".join(reasoning_parts)
        if reasoning.strip():
            final_message["reasoning_content"] = reasoning

        yield {"type": "done", "message": final_message}
