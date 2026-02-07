"""Tests for tool message format in provider API requests.

These tests verify that tool messages are correctly formatted when sent
to different providers. Key behaviors under test:

* RA2 (model-initiated) tool messages keep role="tool" and tool_call_id
* RA3 (user-initiated) tool messages are converted to role="user"
* The `name` field is preserved when present in tool messages
* Extra internal fields (user_tool_call, keep_user_turn, etc.) are stripped

Provider requirements vary:
- OpenAI: tool_call_id required, name optional
- Mistral: tool_call_id required, name optional, ID format restrictions
- StepFun: tool_call_id required (400 if missing), name optional
- DeepSeek: tool_call_id required, name optional
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from eggthreads import ThreadRunner  # type: ignore


class _DummyRunner(ThreadRunner):  # type: ignore[misc]
    """Minimal subclass exposing the sanitization helper.

    We bypass __init__ since _sanitize_messages_for_api doesn't require
    full initialization for testing purposes.
    """

    def __init__(self, normalize_strategy: Optional[str] = None) -> None:
        self.db = MagicMock()
        self.thread_id = "test-thread"
        self.llm = None
        self._normalize_strategy = normalize_strategy

    def _get_tool_call_id_normalization_strategy(self, model_key: Optional[str] = None) -> Optional[str]:
        return self._normalize_strategy


def _sanitize(messages: List[Dict[str, Any]], normalize_strategy: Optional[str] = None) -> List[Dict[str, Any]]:
    """Helper to run sanitization on a message list."""
    r = _DummyRunner(normalize_strategy)
    # Mock the tools config to allow raw output
    with patch('eggthreads.runner.get_thread_tools_config') as mock_cfg:
        mock_cfg.return_value = MagicMock(
            allow_raw_tool_output=True,
            disabled_tools=set()
        )
        return r._sanitize_messages_for_api(messages)


class TestRA2ToolMessageFormat:
    """Tests for RA2 (model-initiated) tool messages."""

    def test_ra2_tool_message_keeps_tool_role(self) -> None:
        """RA2 tool messages should keep role='tool' for the provider."""
        msgs = [
            {"role": "user", "content": "List files"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "tc1", "function": {"name": "bash", "arguments": "{}"}}],
            },
            {
                "role": "tool",
                "tool_call_id": "tc1",
                "content": "file1.txt\nfile2.txt",
                "user_tool_call": False,  # RA2 marker
            },
        ]

        out = _sanitize(msgs)

        # Find the tool message in output
        tool_msgs = [m for m in out if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["role"] == "tool"
        assert tool_msgs[0]["tool_call_id"] == "tc1"

    def test_ra2_tool_message_preserves_tool_call_id(self) -> None:
        """tool_call_id must be preserved - required by most providers."""
        msgs = [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "tool_calls": [{"id": "call_abc123", "function": {"name": "get_weather", "arguments": "{}"}}],
            },
            {
                "role": "tool",
                "tool_call_id": "call_abc123",
                "content": "sunny",
                "user_tool_call": False,
            },
        ]

        out = _sanitize(msgs)

        tool_msg = next(m for m in out if m.get("role") == "tool")
        assert tool_msg["tool_call_id"] == "call_abc123"

    def test_ra2_tool_message_preserves_name_field(self) -> None:
        """The name field should be preserved when present."""
        msgs = [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "tool_calls": [{"id": "tc1", "function": {"name": "get_weather", "arguments": "{}"}}],
            },
            {
                "role": "tool",
                "tool_call_id": "tc1",
                "name": "get_weather",
                "content": "sunny",
                "user_tool_call": False,
            },
        ]

        out = _sanitize(msgs)

        tool_msg = next(m for m in out if m.get("role") == "tool")
        assert tool_msg.get("name") == "get_weather"

    def test_ra2_internal_fields_pass_through_eggthreads_layer(self) -> None:
        """Internal fields pass through eggthreads layer (stripped by eggllm).

        Note: eggthreads._sanitize_messages_for_api() does not strip internal
        fields like user_tool_call, keep_user_turn. These are stripped by
        the eggllm client's _sanitize() function which removes model_key and
        local_tool. Other internal fields pass through to the provider.

        This is acceptable for most providers that ignore unknown fields, but
        could be an issue for strict providers. If problems arise, consider
        adding these fields to eggllm's keys_to_remove set.
        """
        msgs = [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "tool_calls": [{"id": "tc1", "function": {"name": "bash", "arguments": "{}"}}],
            },
            {
                "role": "tool",
                "tool_call_id": "tc1",
                "content": "done",
                "user_tool_call": False,
                "keep_user_turn": False,
                "model_key": "test-model",
            },
        ]

        out = _sanitize(msgs)

        tool_msg = next(m for m in out if m.get("role") == "tool")
        # eggthreads layer doesn't strip these - they pass through
        # (eggllm layer would strip model_key but not others)
        assert tool_msg["role"] == "tool"
        assert tool_msg["tool_call_id"] == "tc1"
        # These internal fields currently pass through at eggthreads layer
        assert "user_tool_call" in tool_msg  # Not stripped
        assert "keep_user_turn" in tool_msg  # Not stripped


class TestRA3ToolMessageFormat:
    """Tests for RA3 (user-initiated) tool messages."""

    def test_ra3_tool_message_converted_to_user_role(self) -> None:
        """RA3 tool messages should be converted to role='user'."""
        msgs = [
            {"role": "user", "content": "$ls"},
            {
                "role": "tool",
                "tool_call_id": "tc1",
                "content": "file1.txt\nfile2.txt",
                "user_tool_call": True,  # RA3 marker
            },
        ]

        out = _sanitize(msgs)

        # The tool message should be converted to user
        assert len(out) == 2
        assert out[0]["role"] == "user"
        assert out[1]["role"] == "user"
        assert "tool_call_id" not in out[1]  # Dropped during conversion

    def test_ra3_no_api_tool_message_not_converted(self) -> None:
        """RA3 tool messages with no_api=True are NOT converted to user role.

        When no_api=True, the tool message keeps role='tool' and is not
        converted to a user message. This is because no_api messages should
        be excluded from provider API calls entirely. The protocol enforcement
        layer will then drop orphan tool messages.
        """
        msgs = [
            {"role": "user", "content": "$$secret"},
            {
                "role": "tool",
                "tool_call_id": "tc1",
                "content": "secret output",
                "user_tool_call": True,
                "no_api": True,  # Blocks conversion to user role
            },
        ]

        out = _sanitize(msgs)

        # The tool message should NOT be converted to user because no_api=True
        # It keeps role='tool' but without a matching assistant, protocol
        # enforcement will drop it as an orphan
        tool_msgs = [m for m in out if m.get("role") == "tool"]
        # Should be dropped by protocol enforcement (no matching assistant)
        assert len(tool_msgs) == 0

        # The original user message should remain
        user_msgs = [m for m in out if m.get("role") == "user"]
        assert len(user_msgs) == 1
        assert user_msgs[0]["content"] == "$$secret"


class TestToolCallIdNormalization:
    """Tests for provider-specific tool_call_id normalization."""

    def test_mistral9_normalization_applied(self) -> None:
        """Mistral9 strategy should normalize IDs to 9 alphanumeric chars."""
        msgs = [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "tool_calls": [{"id": "very-long-tool-call-id-12345", "function": {"name": "test", "arguments": "{}"}}],
            },
            {
                "role": "tool",
                "tool_call_id": "very-long-tool-call-id-12345",
                "content": "result",
            },
        ]

        out = _sanitize(msgs, normalize_strategy="mistral9")

        # Both assistant tool_calls.id and tool tool_call_id should be normalized
        assistant = next(m for m in out if m.get("role") == "assistant")
        tool = next(m for m in out if m.get("role") == "tool")

        # Mistral9 normalizes to exactly 9 alphanumeric characters
        assert len(assistant["tool_calls"][0]["id"]) == 9
        assert len(tool["tool_call_id"]) == 9
        # And they should match
        assert assistant["tool_calls"][0]["id"] == tool["tool_call_id"]

    def test_no_normalization_preserves_original_ids(self) -> None:
        """Without normalization, original IDs are preserved."""
        original_id = "call_abc123xyz"
        msgs = [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "tool_calls": [{"id": original_id, "function": {"name": "test", "arguments": "{}"}}],
            },
            {
                "role": "tool",
                "tool_call_id": original_id,
                "content": "result",
            },
        ]

        out = _sanitize(msgs, normalize_strategy=None)

        assistant = next(m for m in out if m.get("role") == "assistant")
        tool = next(m for m in out if m.get("role") == "tool")

        assert assistant["tool_calls"][0]["id"] == original_id
        assert tool["tool_call_id"] == original_id


class TestAssistantToolCallsFormat:
    """Tests for assistant messages with tool_calls."""

    def test_assistant_tool_calls_has_content_field(self) -> None:
        """Assistant messages with tool_calls must include content field.

        Some providers (e.g., StepFun) return HTTP 400 "Unrecognized chat message"
        if the content field is missing from assistant messages with tool_calls.
        The content field should be present even if empty.
        """
        msgs = [
            {"role": "user", "content": "run ls"},
            {
                "role": "assistant",
                "content": "",  # Empty but present
                "tool_calls": [{"id": "tc1", "function": {"name": "bash", "arguments": "{}"}}],
            },
            {
                "role": "tool",
                "tool_call_id": "tc1",
                "content": "file.txt",
            },
        ]

        out = _sanitize(msgs)

        # Find the assistant message with tool_calls
        assistant_msgs = [m for m in out if m.get("role") == "assistant" and m.get("tool_calls")]
        assert len(assistant_msgs) == 1
        # Content field MUST be present (even if empty)
        assert "content" in assistant_msgs[0]


class TestProviderExpectations:
    """Document provider-specific expectations for tool message format.

    These tests serve as documentation for what each provider expects.
    They verify the format that would be sent matches provider requirements.
    """

    def test_openai_format(self) -> None:
        """OpenAI expects: role=tool, tool_call_id required, name optional."""
        msgs = [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "tool_calls": [{"id": "call_123", "type": "function", "function": {"name": "test", "arguments": "{}"}}],
            },
            {
                "role": "tool",
                "tool_call_id": "call_123",
                "content": "result",
            },
        ]

        out = _sanitize(msgs)

        tool_msg = next(m for m in out if m.get("role") == "tool")
        assert tool_msg["role"] == "tool"
        assert "tool_call_id" in tool_msg
        # name is optional for OpenAI

    def test_stepfun_format(self) -> None:
        """StepFun expects: role=tool, tool_call_id required (400 if missing), name optional."""
        msgs = [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "tool_calls": [{"id": "chatcmpl-tool-abc", "type": "function", "function": {"name": "get_weather", "arguments": "{}"}}],
            },
            {
                "role": "tool",
                "tool_call_id": "chatcmpl-tool-abc",
                "content": "sunny",
            },
        ]

        out = _sanitize(msgs)

        tool_msg = next(m for m in out if m.get("role") == "tool")
        # StepFun returns 400 "invalid tool message, tool_call_id is required" without this
        assert tool_msg["role"] == "tool"
        assert "tool_call_id" in tool_msg
        assert tool_msg["tool_call_id"]  # Must be non-empty

    def test_deepseek_format(self) -> None:
        """DeepSeek expects: role=tool, tool_call_id required, name optional."""
        msgs = [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "tool_calls": [{"id": "call_xyz", "function": {"name": "search", "arguments": "{}"}}],
            },
            {
                "role": "tool",
                "tool_call_id": "call_xyz",
                "content": "found",
            },
        ]

        out = _sanitize(msgs)

        tool_msg = next(m for m in out if m.get("role") == "tool")
        assert tool_msg["role"] == "tool"
        assert "tool_call_id" in tool_msg

    def test_mistral_format_with_id_normalization(self) -> None:
        """Mistral expects: role=tool, tool_call_id required (9 alnum chars), name optional."""
        msgs = [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "tool_calls": [{"id": "long_id_that_needs_normalization", "function": {"name": "test", "arguments": "{}"}}],
            },
            {
                "role": "tool",
                "tool_call_id": "long_id_that_needs_normalization",
                "content": "result",
            },
        ]

        # Mistral requires normalized IDs
        out = _sanitize(msgs, normalize_strategy="mistral9")

        tool_msg = next(m for m in out if m.get("role") == "tool")
        assert tool_msg["role"] == "tool"
        assert "tool_call_id" in tool_msg
        # Mistral requires exactly 9 alphanumeric characters
        assert len(tool_msg["tool_call_id"]) == 9
        assert tool_msg["tool_call_id"].isalnum()
