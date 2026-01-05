"""Shared pytest fixtures for egg app tests."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

# Ensure project root is in path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Add sibling libraries to path (eggthreads, eggllm, eggdisplay)
SIBLING_LIBS = ['eggthreads', 'eggllm', 'eggdisplay']
for lib in SIBLING_LIBS:
    lib_path = str(PROJECT_ROOT.parent / lib)
    if lib_path not in sys.path:
        sys.path.insert(0, lib_path)


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Create an isolated ThreadsDB for unit tests.

    Uses tmp_path to ensure database is created in temporary directory,
    preventing test pollution.
    """
    monkeypatch.chdir(tmp_path)
    from eggthreads import ThreadsDB
    db = ThreadsDB()
    db.init_schema()
    return db


@pytest.fixture
def egg_app(tmp_path, monkeypatch):
    """Create an isolated EggDisplayApp instance for testing.

    Disables scheduler and aiohttp requirements for simpler testing.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EGG_FORCE_WITHOUT_AIOHTTP", "1")

    import egg

    # Disable scheduler to avoid async complications in tests
    monkeypatch.setattr(egg.EggDisplayApp, "start_scheduler", lambda self, root_tid: None)

    app = egg.EggDisplayApp()
    return app


@pytest.fixture
def thread_with_messages(isolated_db):
    """Create a thread with sample messages for formatting/display tests.

    Returns tuple of (db, thread_id) with system, user, and assistant messages.
    """
    from eggthreads import create_root_thread, append_message, create_snapshot

    tid = create_root_thread(isolated_db, name="TestThread")
    append_message(isolated_db, tid, "system", "You are a helpful assistant.")
    append_message(isolated_db, tid, "user", "Hello!")
    append_message(isolated_db, tid, "assistant", "Hi there! How can I help you today?")
    create_snapshot(isolated_db, tid)

    return isolated_db, tid


@pytest.fixture
def thread_with_tool_calls(isolated_db):
    """Create a thread with tool calls for approval testing.

    Returns tuple of (db, thread_id, tool_call_id).
    """
    from eggthreads import create_root_thread, append_message, create_snapshot

    tid = create_root_thread(isolated_db, name="ToolTestThread")
    append_message(isolated_db, tid, "system", "You are a helpful assistant.")
    append_message(isolated_db, tid, "user", "Run a command for me")

    # Create assistant message with tool calls
    tc_id = "tc_test_001"
    tool_calls = [{
        "id": tc_id,
        "type": "function",
        "function": {
            "name": "bash",
            "arguments": json.dumps({"script": "echo hello"})
        }
    }]
    append_message(
        isolated_db, tid, "assistant", "",
        extra={"tool_calls": tool_calls}
    )
    create_snapshot(isolated_db, tid)

    return isolated_db, tid, tc_id


@pytest.fixture
def mock_llm_client():
    """Create a mock LLM client for model completion tests."""

    class MockRegistry:
        models_config = {
            "gpt-4": {"provider": "openai", "alias": ["gpt4"]},
            "claude-3": {"provider": "anthropic", "alias": ["claude"]},
            "local-model": {"provider": "local", "alias": []},
        }

    class MockCatalog:
        def get_all_models_suggestions(self, prefix: str) -> List[str]:
            return ["all:openai:gpt-4", "all:anthropic:claude-3-opus"]

        def get_all_models_for_provider(self, provider: str) -> List[str]:
            if provider == "openai":
                return ["gpt-4", "gpt-3.5-turbo"]
            elif provider == "anthropic":
                return ["claude-3-opus", "claude-3-sonnet"]
            return []

    class MockLLM:
        registry = MockRegistry()
        catalog = MockCatalog()

        def get_providers(self) -> List[str]:
            return ["openai", "anthropic", "local"]

    return MockLLM()


@pytest.fixture
def mock_input_panel():
    """Create a mock InputPanel for input handling tests."""

    class MockCursor:
        row = 0
        col = 0

    class MockInnerEditor:
        def __init__(self):
            self._text = ""
            self._completion_active = False
            self._completion_items = []
            self._completion_index = 0
            self.cursor = MockCursor()

        def set_text(self, t: str) -> None:
            self._text = t

        def handle_key(self, k: str) -> None:
            pass

        def insert_newline(self) -> None:
            self._text += "\n"

        def _clamp_cursor(self) -> None:
            pass

    class MockEditor:
        def __init__(self):
            self.editor = MockInnerEditor()
            self.running = True
            self.input_queue = None

        def _handle_key(self, key: str) -> bool:
            return True

    class MockInputPanel:
        def __init__(self):
            self.editor = MockEditor()
            self._scroll_top = 0
            self._hscroll_left = 0
            self._message_count = 0

        def get_text(self) -> str:
            return self.editor.editor._text

        def clear_text(self) -> None:
            self.editor.editor._text = ""

        def increment_message_count(self) -> None:
            self._message_count += 1

    return MockInputPanel()


@pytest.fixture
def captured_logs():
    """Fixture for capturing system log output.

    Returns a list that test can check for logged messages.
    """
    return []


def uid() -> str:
    """Generate a unique ID for test events."""
    import uuid
    return uuid.uuid4().hex
