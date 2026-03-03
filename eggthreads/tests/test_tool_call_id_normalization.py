"""Tests for tool_call_id normalization.

These tests verify that:
1. The normalization function works correctly for different strategies
2. Normalization in _sanitize_messages_for_api doesn't mutate original messages
3. Tool call state tracking continues to work correctly with normalized IDs
"""
from __future__ import annotations

import copy
import json
import tempfile
from typing import Any, Dict, List, Optional

import pytest

from eggthreads.tool_call_id import normalize_tool_call_id
from eggthreads.db import ThreadsDB
from eggthreads.tool_state import build_tool_call_states


class TestNormalizeToolCallId:
    """Unit tests for normalize_tool_call_id function."""

    def test_passthrough_when_no_strategy(self) -> None:
        """No normalization when strategy is None or empty."""
        original = "GYHR5MqgHVrKEwoiuqJfJA6tWZCUh8dH"
        assert normalize_tool_call_id(original, None) == original
        assert normalize_tool_call_id(original, "") == original

    def test_passthrough_for_unknown_strategy(self) -> None:
        """Unknown strategies pass through unchanged."""
        original = "abc123"
        assert normalize_tool_call_id(original, "unknown_strategy") == original

    def test_mistral9_already_valid(self) -> None:
        """Already valid 9-char alphanumeric IDs pass through."""
        valid_id = "abc123DEF"
        assert normalize_tool_call_id(valid_id, "mistral9") == valid_id

    def test_mistral9_normalizes_long_id(self) -> None:
        """Long IDs are normalized to exactly 9 alphanumeric chars."""
        original = "GYHR5MqgHVrKEwoiuqJfJA6tWZCUh8dH"  # 32 chars
        result = normalize_tool_call_id(original, "mistral9")
        assert len(result) == 9
        assert result.isalnum()

    def test_mistral9_deterministic(self) -> None:
        """Same input always produces same output."""
        original = "GYHR5MqgHVrKEwoiuqJfJA6tWZCUh8dH"
        result1 = normalize_tool_call_id(original, "mistral9")
        result2 = normalize_tool_call_id(original, "mistral9")
        assert result1 == result2

    def test_mistral9_different_inputs_different_outputs(self) -> None:
        """Different inputs produce different outputs."""
        id1 = "GYHR5MqgHVrKEwoiuqJfJA6tWZCUh8dH"
        id2 = "p19Dfpn6TBjfm8YKpAX55TiIuR3hpNeR"
        result1 = normalize_tool_call_id(id1, "mistral9")
        result2 = normalize_tool_call_id(id2, "mistral9")
        assert result1 != result2

    def test_mistral9_handles_special_chars(self) -> None:
        """IDs with special chars (like our fallback format) are normalized."""
        fallback_id = "msg123abc:0"  # Contains colon
        result = normalize_tool_call_id(fallback_id, "mistral9")
        assert len(result) == 9
        assert result.isalnum()


class TestSanitizeMessagesNoMutation:
    """Test that _sanitize_messages_for_api doesn't mutate original messages.

    This is a regression test for a bug where shallow copying of messages
    caused the original tool_calls dicts to be mutated during normalization,
    which broke tool call state tracking.
    """

    def test_normalization_does_not_mutate_original_messages(self, tmp_path) -> None:
        """Verify that normalizing tool_call_ids doesn't mutate input messages.

        Regression test: Previously, the shallow copy m2 = dict(m) caused
        modifications to m2["tool_calls"][i]["id"] to also modify the original
        message, breaking state tracking.
        """
        from eggthreads.runner import ThreadRunner

        original_id = "GYHR5MqgHVrKEwoiuqJfJA6tWZCUh8dH"

        messages = [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": "I will call a tool.",
                "tool_calls": [
                    {"id": original_id, "type": "function", "function": {"name": "bash", "arguments": "{}"}},
                ],
            },
            {"role": "tool", "tool_call_id": original_id, "content": "ok"},
        ]

        # Deep copy to compare later
        messages_before = copy.deepcopy(messages)

        # Create a minimal runner that returns "mistral9" strategy
        db = ThreadsDB(tmp_path / "test.sqlite")
        db.init_schema()
        db.create_thread("test-thread")

        class TestRunner(ThreadRunner):
            def __init__(self):
                self.db = db
                self.thread_id = "test-thread"
                self.llm = None

            def _get_tool_call_id_normalization_strategy(self, model_key):
                return "mistral9"

        runner = TestRunner()
        sanitized = runner._sanitize_messages_for_api(messages, model_key="test")

        # Original messages should be unchanged
        assert messages == messages_before, "Original messages were mutated!"

        # But sanitized messages should have normalized IDs
        normalized_id = normalize_tool_call_id(original_id, "mistral9")
        assert sanitized[1]["tool_calls"][0]["id"] == normalized_id
        assert sanitized[2]["tool_call_id"] == normalized_id

    def test_no_normalization_without_strategy(self, tmp_path) -> None:
        """Without a strategy, IDs should pass through unchanged."""
        from eggthreads.runner import ThreadRunner

        original_id = "GYHR5MqgHVrKEwoiuqJfJA6tWZCUh8dH"

        messages = [
            {
                "role": "assistant",
                "tool_calls": [{"id": original_id, "function": {"name": "bash", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": original_id, "content": "ok"},
        ]

        db = ThreadsDB(tmp_path / "test.sqlite")
        db.init_schema()
        db.create_thread("test-thread")

        class TestRunner(ThreadRunner):
            def __init__(self):
                self.db = db
                self.thread_id = "test-thread"
                self.llm = None

            def _get_tool_call_id_normalization_strategy(self, model_key):
                return None  # No strategy

        runner = TestRunner()
        sanitized = runner._sanitize_messages_for_api(messages, model_key="test")

        # IDs should be unchanged
        assert sanitized[0]["tool_calls"][0]["id"] == original_id
        assert sanitized[1]["tool_call_id"] == original_id


class TestToolCallStateTracking:
    """Test that tool call state tracking works with original IDs."""

    def _make_db(self, tmp_path):
        """Create a test database with initialized schema."""
        db = ThreadsDB(tmp_path / "threads.sqlite")
        db.init_schema()
        return db

    def _append_event(self, db, tid, eid, type_, payload, msg_id=None):
        db.append_event(
            event_id=eid,
            thread_id=tid,
            type_=type_,
            payload=payload,
            msg_id=msg_id,
        )

    def test_tool_call_state_uses_original_ids(self, tmp_path) -> None:
        """Verify build_tool_call_states matches tool calls by original IDs."""
        db = self._make_db(tmp_path)
        tid = "test-thread-001"
        db.create_thread(tid)

        # Use a long ID that would be normalized for Mistral
        original_tc_id = "GYHR5MqgHVrKEwoiuqJfJA6tWZCUh8dH"

        # 1. User message
        self._append_event(db, tid, "ev1", "msg.create",
                          {"role": "user", "content": "hello"}, msg_id="m-user")

        # 2. Assistant message with tool_call
        self._append_event(db, tid, "ev2", "msg.create", {
            "role": "assistant",
            "content": "I'll run a command",
            "tool_calls": [
                {"id": original_tc_id, "type": "function", "function": {"name": "bash", "arguments": "{}"}}
            ]
        }, msg_id="m-assistant")

        # Check state before tool result - should be TC1 (needs approval)
        states = build_tool_call_states(db, tid)
        assert original_tc_id in states
        assert states[original_tc_id].state == "TC1"

        # 3. Approval event
        self._append_event(db, tid, "ev3", "tool_call.approval",
                          {"tool_call_id": original_tc_id, "decision": "granted"})

        # 4. Execution started
        self._append_event(db, tid, "ev4", "tool_call.execution_started",
                          {"tool_call_id": original_tc_id})

        # 5. Finished
        self._append_event(db, tid, "ev5", "tool_call.finished",
                          {"tool_call_id": original_tc_id, "reason": "success", "output": "done"})

        # 6. Output approval
        self._append_event(db, tid, "ev6", "tool_call.output_approval",
                          {"tool_call_id": original_tc_id, "decision": "whole"})

        # 7. Tool result message (using original ID, as the runner would)
        self._append_event(db, tid, "ev7", "msg.create", {
            "role": "tool",
            "content": "command output",
            "tool_call_id": original_tc_id,  # Original ID, not normalized
        }, msg_id="m-tool")

        # Rebuild states - tool call should be marked as published (TC6)
        states = build_tool_call_states(db, tid)
        assert original_tc_id in states
        assert states[original_tc_id].state == "TC6", \
            f"Expected TC6 (published), got {states[original_tc_id].state}"
        assert states[original_tc_id].published is True

    def test_multiple_tool_calls_tracked_independently(self, tmp_path) -> None:
        """Multiple tool calls with different IDs are tracked separately."""
        db = self._make_db(tmp_path)
        tid = "test-thread-002"
        db.create_thread(tid)

        tc_id_1 = "AAAA5MqgHVrKEwoiuqJfJA6tWZCUh8dH"
        tc_id_2 = "BBBB5MqgHVrKEwoiuqJfJA6tWZCUh8dH"

        # User message
        self._append_event(db, tid, "ev1", "msg.create",
                          {"role": "user", "content": "hello"}, msg_id="m-user")

        # Assistant with two tool calls
        self._append_event(db, tid, "ev2", "msg.create", {
            "role": "assistant",
            "tool_calls": [
                {"id": tc_id_1, "type": "function", "function": {"name": "bash", "arguments": "{}"}},
                {"id": tc_id_2, "type": "function", "function": {"name": "bash", "arguments": "{}"}},
            ]
        }, msg_id="m-assistant")

        states = build_tool_call_states(db, tid)
        assert tc_id_1 in states
        assert tc_id_2 in states
        assert states[tc_id_1].state == "TC1"
        assert states[tc_id_2].state == "TC1"

        # Complete first tool call only
        for ev_id, ev_type, payload in [
            ("ev3", "tool_call.approval", {"tool_call_id": tc_id_1, "decision": "granted"}),
            ("ev4", "tool_call.execution_started", {"tool_call_id": tc_id_1}),
            ("ev5", "tool_call.finished", {"tool_call_id": tc_id_1, "reason": "success"}),
            ("ev6", "tool_call.output_approval", {"tool_call_id": tc_id_1, "decision": "whole"}),
        ]:
            self._append_event(db, tid, ev_id, ev_type, payload)

        self._append_event(db, tid, "ev7", "msg.create", {
            "role": "tool",
            "content": "output 1",
            "tool_call_id": tc_id_1,
        }, msg_id="m-tool-1")

        # Check states - first should be published, second still needs approval
        states = build_tool_call_states(db, tid)
        assert states[tc_id_1].state == "TC6"
        assert states[tc_id_2].state == "TC1"
