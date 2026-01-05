"""Tests for model.switch events and concrete model info."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, Mock
import pytest


def _import_eggthreads(monkeypatch, tmp_path: Path):
    """Import eggthreads from the monorepo checkout, isolated to tmp_path."""
    monkeypatch.chdir(tmp_path)
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    import eggthreads  # noqa: F401
    return sys.modules["eggthreads"]


@pytest.fixture
def eggthreads(monkeypatch, tmp_path):
    """Fixture to import eggthreads with isolated environment."""
    return _import_eggthreads(monkeypatch, tmp_path)


def test_set_thread_model_without_concrete_info(eggthreads, tmp_path):
    """Test basic model.switch event creation without concrete info."""
    from eggthreads import ThreadsDB, set_thread_model, current_thread_model, current_thread_model_info

    db_path = tmp_path / "threads.sqlite"
    db = ThreadsDB(db_path)
    db.init_schema()

    tid = "thread-1"
    db.create_thread(thread_id=tid, name="test", parent_id=None, initial_model_key=None, depth=0)

    # Mock eggllm to be unavailable
    with patch('eggthreads.api.EGGLLM_AVAILABLE', False):
        set_thread_model(db, tid, "GPT-4", reason="test")

    # Verify event
    events = db.conn.execute(
        "SELECT payload_json FROM events WHERE thread_id=? AND type='model.switch' ORDER BY event_seq DESC",
        (tid,)
    ).fetchall()
    assert len(events) == 1
    payload = json.loads(events[0][0])
    assert payload['model_key'] == 'GPT-4'
    assert payload['reason'] == 'test'
    assert 'concrete_model_info' not in payload

    # Verify current_thread_model returns correct key
    assert current_thread_model(db, tid) == 'GPT-4'
    # concrete info should be None
    assert current_thread_model_info(db, tid) is None


def test_set_thread_model_with_concrete_info(eggthreads, tmp_path):
    """Test model.switch event with explicit concrete_model_info."""
    from eggthreads import ThreadsDB, set_thread_model, current_thread_model, current_thread_model_info

    db_path = tmp_path / "threads.sqlite"
    db = ThreadsDB(db_path)
    db.init_schema()

    tid = "thread-1"
    db.create_thread(thread_id=tid, name="test", parent_id=None, initial_model_key=None, depth=0)

    concrete = {
        "providers": {
            "openai": {
                "api_base": "https://api.openai.com/v1/chat/completions",
                "api_key_env": "OPENAI_API_KEY",
                "models": {
                    "GPT-4": {
                        "model_name": "gpt-4",
                        "max_tokens": 4096,
                        "cost": {"input_tokens": 0.03, "output_tokens": 0.06}
                    }
                }
            }
        }
    }

    set_thread_model(db, tid, "GPT-4", concrete_model_info=concrete, reason="test")

    events = db.conn.execute(
        "SELECT payload_json FROM events WHERE thread_id=? AND type='model.switch' ORDER BY event_seq DESC",
        (tid,)
    ).fetchall()
    assert len(events) == 1
    payload = json.loads(events[0][0])
    assert payload['model_key'] == 'GPT-4'
    assert payload['reason'] == 'test'
    assert 'concrete_model_info' in payload
    assert payload['concrete_model_info'] == concrete

    # Verify current_thread_model_info returns the concrete info
    retrieved = current_thread_model_info(db, tid)
    assert retrieved == concrete


def test_current_thread_model_precedence(eggthreads, tmp_path):
    """Test precedence: model.switch event overrides initial_model_key."""
    from eggthreads import ThreadsDB, set_thread_model, current_thread_model

    db_path = tmp_path / "threads.sqlite"
    db = ThreadsDB(db_path)
    db.init_schema()

    # Create thread directly without initial_model_key to test manual switching
    tid = "thread-1"
    db.create_thread(thread_id=tid, name="test", parent_id=None, initial_model_key=None, depth=0)

    # No model.switch events yet, should be None
    assert current_thread_model(db, tid) is None

    # Add a model.switch event
    set_thread_model(db, tid, "switched-model", reason="test")

    # Now should use the switched model
    assert current_thread_model(db, tid) == 'switched-model'


def test_model_inheritance_subthreads(eggthreads, tmp_path):
    """Test that child threads do NOT inherit model from parent - they need explicit model.switch."""
    from eggthreads import ThreadsDB, set_thread_model, current_thread_model, current_thread_model_info

    db_path = tmp_path / "threads.sqlite"
    db = ThreadsDB(db_path)
    db.init_schema()

    parent = "parent-1"
    db.create_thread(thread_id=parent, name="parent", parent_id=None, initial_model_key=None, depth=0)

    # Set model on parent with concrete info
    concrete = {
        "providers": {
            "openai": {
                "api_base": "https://api.openai.com/v1/chat/completions",
                "api_key_env": "OPENAI_API_KEY",
                "models": {
                    "GPT-4": {
                        "model_name": "gpt-4",
                        "max_tokens": 4096,
                    }
                }
            }
        }
    }
    set_thread_model(db, parent, "GPT-4", concrete_model_info=concrete, reason="parent")

    # Create child without explicit initial_model_key (using db.create_thread directly to avoid auto model.switch)
    child = "child-1"
    db.create_thread(thread_id=child, name="child", parent_id=parent, initial_model_key=None, depth=1)

    # Child should NOT inherit model automatically (inheritance is not automatic;
    # child must have its own model.switch event or initial_model_key).
    assert current_thread_model(db, child) is None
    assert current_thread_model_info(db, child) is None


def test_initial_model_creates_switch_event(eggthreads, tmp_path):
    """Test that create_root_thread with initial_model_key creates a model.switch event."""
    from eggthreads import ThreadsDB, create_root_thread, current_thread_model, current_thread_model_info

    db_path = tmp_path / "threads.sqlite"
    db = ThreadsDB(db_path)
    db.init_schema()

    # Create a minimal models.json for testing
    models_json = tmp_path / "models.json"
    models_json.write_text(json.dumps({
        "default_model": "TestModel",
        "providers": {
            "openai": {
                "api_base": "https://api.openai.com/v1/chat/completions",
                "api_key_env": "OPENAI_API_KEY",
                "models": {
                    "TestModel": {
                        "model_name": "gpt-4",
                        "max_tokens": 4096,
                        "cost": {"input_tokens": 0.03, "output_tokens": 0.06}
                    }
                }
            }
        }
    }))

    # Create thread with initial_model_key - should automatically create model.switch event
    tid = create_root_thread(db, name="test", initial_model_key="TestModel", models_path=str(models_json))

    # Verify model.switch event was created
    events = db.conn.execute(
        "SELECT payload_json FROM events WHERE thread_id=? AND type='model.switch'",
        (tid,)
    ).fetchall()
    assert len(events) == 1
    payload = json.loads(events[0][0])
    assert payload['model_key'] == 'TestModel'
    assert payload['reason'] == 'initial'

    # Verify concrete_model_info was populated
    assert 'concrete_model_info' in payload
    concrete = payload['concrete_model_info']
    assert 'providers' in concrete
    assert 'openai' in concrete['providers']
    assert 'TestModel' in concrete['providers']['openai']['models']

    # Verify current_thread_model returns correct value
    assert current_thread_model(db, tid) == 'TestModel'

    # Verify current_thread_model_info returns concrete info
    info = current_thread_model_info(db, tid)
    assert info is not None
    assert info == concrete


def test_child_thread_initial_model_creates_switch_event(eggthreads, tmp_path):
    """Test that create_child_thread with initial_model_key creates a model.switch event."""
    from eggthreads import ThreadsDB, create_root_thread, create_child_thread, current_thread_model, current_thread_model_info

    db_path = tmp_path / "threads.sqlite"
    db = ThreadsDB(db_path)
    db.init_schema()

    # Create a minimal models.json for testing
    models_json = tmp_path / "models.json"
    models_json.write_text(json.dumps({
        "default_model": "TestModel",
        "providers": {
            "openai": {
                "api_base": "https://api.openai.com/v1/chat/completions",
                "api_key_env": "OPENAI_API_KEY",
                "models": {
                    "TestModel": {
                        "model_name": "gpt-4",
                        "max_tokens": 4096,
                    },
                    "ChildModel": {
                        "model_name": "gpt-4-child",
                        "max_tokens": 2048,
                    }
                }
            }
        }
    }))

    # Create parent thread
    parent_tid = create_root_thread(db, name="parent", models_path=str(models_json))

    # Create child thread with initial_model_key
    child_tid = create_child_thread(db, parent_tid, name="child", initial_model_key="ChildModel", models_path=str(models_json))

    # Verify model.switch event was created for child
    events = db.conn.execute(
        "SELECT payload_json FROM events WHERE thread_id=? AND type='model.switch'",
        (child_tid,)
    ).fetchall()
    assert len(events) == 1
    payload = json.loads(events[0][0])
    assert payload['model_key'] == 'ChildModel'
    assert payload['reason'] == 'initial'
    assert 'concrete_model_info' in payload


def test_models_from_events_without_models_json(eggthreads, tmp_path):
    """Test that a model.switch event with full concrete info can be used even if model not in models.json."""
    from eggthreads import ThreadsDB, set_thread_model, current_thread_model, current_thread_model_info
    from eggthreads.runner import ThreadRunner

    db_path = tmp_path / "threads.sqlite"
    db = ThreadsDB(db_path)
    db.init_schema()

    tid = "thread-1"
    db.create_thread(thread_id=tid, name="test", parent_id=None, initial_model_key=None, depth=0)

    # Create concrete_model_info for a model that doesn't exist in any models.json
    concrete = {
        "providers": {
            "custom_provider": {
                "api_base": "https://custom-api.example.com/v1/chat",
                "api_key_env": "CUSTOM_API_KEY",
                "models": {
                    "CustomModel-v99": {
                        "model_name": "custom-model-v99",
                        "max_tokens": 100000,
                        "cost": {"input_tokens": 0.01, "output_tokens": 0.02}
                    }
                }
            }
        }
    }

    # Set model with concrete info directly (model doesn't exist in models.json)
    set_thread_model(db, tid, "CustomModel-v99", concrete_model_info=concrete, reason="test")

    # Verify the model is retrievable
    assert current_thread_model(db, tid) == "CustomModel-v99"
    assert current_thread_model_info(db, tid) == concrete

    # Create mock LLMClient that tracks calls
    mock_llm = Mock()
    mock_llm.current_model_key = None
    mock_llm.set_model_with_config = Mock(return_value="CustomModel-v99")
    mock_llm.set_model = Mock()

    # Create runner with mocked llm
    runner = ThreadRunner(
        db=db,
        thread_id=tid,
        llm=mock_llm,
        owner="test"
    )

    # Add a user message to make the thread runnable (RA1)
    from eggthreads import append_message
    append_message(db, tid, "user", "Hello")

    # Mock the streaming to avoid actual API calls
    async def mock_astream_chat(*args, **kwargs):
        yield {"type": "content_delta", "text": "Hi"}
        yield {"type": "done", "message": {"role": "assistant", "content": "Hi"}}

    mock_llm.astream_chat = mock_astream_chat

    # Run the runner
    import asyncio
    result = asyncio.run(runner.run_once())

    # Verify set_model_with_config was called with the model key and concrete info
    mock_llm.set_model_with_config.assert_called_once_with("CustomModel-v99", concrete)
    # Verify set_model was NOT called (it would override set_model_with_config)
    mock_llm.set_model.assert_not_called()


def test_runner_uses_concrete_model_info(eggthreads, tmp_path):
    """Integration test: ThreadRunner should use concrete_model_info when setting model."""
    from eggthreads import ThreadsDB, set_thread_model, current_thread_model_info, append_message
    from eggthreads.runner import ThreadRunner

    db_path = tmp_path / "threads.sqlite"
    db = ThreadsDB(db_path)
    db.init_schema()

    tid = "thread-1"
    db.create_thread(thread_id=tid, name="test", parent_id=None, initial_model_key=None, depth=0)

    concrete = {
        "providers": {
            "openai": {
                "api_base": "https://api.openai.com/v1/chat/completions",
                "api_key_env": "OPENAI_API_KEY",
                "models": {
                    "MyCustomModel": {
                        "model_name": "custom-gpt",
                        "max_tokens": 2048
                    }
                }
            }
        }
    }

    set_thread_model(db, tid, "MyCustomModel", concrete_model_info=concrete, reason="test")

    # Verify concrete info is stored
    retrieved = current_thread_model_info(db, tid)
    assert retrieved == concrete

    # Create mock LLMClient
    mock_llm = Mock()
    mock_llm.current_model_key = None
    mock_llm.set_model_with_config = Mock(return_value="MyCustomModel")
    mock_llm.set_model = Mock()

    async def mock_astream_chat(*args, **kwargs):
        yield {"type": "content_delta", "text": "Response"}
        yield {"type": "done", "message": {"role": "assistant", "content": "Response"}}

    mock_llm.astream_chat = mock_astream_chat

    # Create runner
    runner = ThreadRunner(
        db=db,
        thread_id=tid,
        llm=mock_llm,
        owner="test"
    )

    # Add user message to trigger RA1
    append_message(db, tid, "user", "Hello")

    # Run and verify
    import asyncio
    asyncio.run(runner.run_once())

    # Verify set_model_with_config was called correctly
    mock_llm.set_model_with_config.assert_called_once_with("MyCustomModel", concrete)


def test_model_switch_inheritance_with_concrete_info(eggthreads, tmp_path):
    """Test that child threads can have their own concrete_model_info independent of parent."""
    from eggthreads import ThreadsDB, set_thread_model, current_thread_model, current_thread_model_info

    db_path = tmp_path / "threads.sqlite"
    db = ThreadsDB(db_path)
    db.init_schema()

    # Create parent with one model
    parent = "parent-1"
    db.create_thread(thread_id=parent, name="parent", parent_id=None, initial_model_key=None, depth=0)

    parent_concrete = {
        "providers": {
            "openai": {
                "api_base": "https://api.openai.com/v1/chat/completions",
                "api_key_env": "OPENAI_API_KEY",
                "models": {
                    "ParentModel": {
                        "model_name": "gpt-4",
                        "max_tokens": 8000,
                    }
                }
            }
        }
    }
    set_thread_model(db, parent, "ParentModel", concrete_model_info=parent_concrete, reason="parent")

    # Create child with different model
    child = "child-1"
    db.create_thread(thread_id=child, name="child", parent_id=parent, initial_model_key=None, depth=1)

    child_concrete = {
        "providers": {
            "anthropic": {
                "api_base": "https://api.anthropic.com/v1/messages",
                "api_key_env": "ANTHROPIC_API_KEY",
                "models": {
                    "ChildModel": {
                        "model_name": "claude-3-opus",
                        "max_tokens": 200000,
                    }
                }
            }
        }
    }
    set_thread_model(db, child, "ChildModel", concrete_model_info=child_concrete, reason="child")

    # Verify parent and child have independent model configs
    assert current_thread_model(db, parent) == "ParentModel"
    assert current_thread_model_info(db, parent) == parent_concrete

    assert current_thread_model(db, child) == "ChildModel"
    assert current_thread_model_info(db, child) == child_concrete

    # Verify they are different
    assert current_thread_model(db, parent) != current_thread_model(db, child)
    assert current_thread_model_info(db, parent) != current_thread_model_info(db, child)


if __name__ == '__main__':
    pytest.main([__file__])
