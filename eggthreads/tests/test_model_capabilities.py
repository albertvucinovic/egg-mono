from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import eggthreads as ts


def _write_models(tmp_path: Path) -> Path:
    models = {
        "default_model": "Image Generator",
        "providers": {
            "openai": {
                "api_base": "https://api.openai.com/v1/chat/completions",
                "api_key_env": "OPENAI_API_KEY",
                "models": {
                    "Image Generator": {"model_name": "gpt-image-1", "model_kind": "image_generation"},
                    "Chat": {"model_name": "gpt-4o"},
                },
            }
        },
    }
    path = tmp_path / "models.json"
    path.write_text(json.dumps(models), encoding="utf-8")
    return path


def test_create_root_thread_skips_non_chat_default_model(tmp_path, monkeypatch):
    monkeypatch.delenv("EGG_STARTING_MODEL", raising=False)
    models_path = _write_models(tmp_path)
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()

    tid = ts.create_root_thread(db, name="root", models_path=str(models_path))

    assert ts.current_thread_model(db, tid) == "Chat"


def test_set_thread_model_rejects_non_chat_model_kind(tmp_path):
    models_path = _write_models(tmp_path)
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="root", initial_model_key="Chat", models_path=str(models_path))

    with pytest.raises(ValueError, match="model_kind 'image_generation'"):
        ts.set_thread_model(db, tid, "Image Generator", models_path=str(models_path))

    assert ts.current_thread_model(db, tid) == "Chat"


def test_set_thread_model_rejects_unknown_model_when_models_config_exists(tmp_path):
    models_path = _write_models(tmp_path)
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="root", initial_model_key="Chat", models_path=str(models_path))

    with pytest.raises(ValueError, match="Unknown model: Missing Model"):
        ts.set_thread_model(db, tid, "Missing Model", models_path=str(models_path))

    assert ts.current_thread_model(db, tid) == "Chat"


def test_set_thread_model_rejects_unknown_model_with_explicit_missing_models_path(tmp_path):
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="root")

    with pytest.raises(ValueError, match="Unknown model: Missing Model"):
        ts.set_thread_model(db, tid, "Missing Model", models_path=str(tmp_path / "missing-models.json"))

    assert ts.current_thread_model(db, tid) is None


def test_set_thread_model_rejects_provider_level_non_chat_model_kind(tmp_path):
    models = {
        "providers": {
            "openai-images": {
                "api_base": "https://api.openai.com/v1/images/generations",
                "api_key_env": "OPENAI_API_KEY",
                "model_kind": "image_generation",
                "models": {"Image Backend": {"model_name": "gpt-image-1"}},
            }
        },
    }
    models_path = tmp_path / "models.json"
    models_path.write_text(json.dumps(models), encoding="utf-8")
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="root", models_path=str(models_path))

    assert ts.current_thread_model(db, tid) is None
    with pytest.raises(ValueError, match="model_kind 'image_generation'"):
        ts.set_thread_model(db, tid, "Image Backend", models_path=str(models_path))


def test_create_root_thread_skips_provider_level_non_chat_string_model(tmp_path, monkeypatch):
    monkeypatch.delenv("EGG_STARTING_MODEL", raising=False)
    models = {
        "default_model": "Image Backend",
        "providers": {
            "openai-images": {
                "api_base": "https://api.openai.com/v1/images/generations",
                "api_key_env": "OPENAI_API_KEY",
                "model_kind": "image_generation",
                "models": {"Image Backend": "gpt-image-1"},
            },
            "openai": {
                "api_base": "https://api.openai.com/v1/chat/completions",
                "api_key_env": "OPENAI_API_KEY",
                "models": {"Chat": "gpt-4o"},
            },
        },
    }
    models_path = tmp_path / "models.json"
    models_path.write_text(json.dumps(models), encoding="utf-8")
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()

    tid = ts.create_root_thread(db, name="root", models_path=str(models_path))

    assert ts.current_thread_model(db, tid) == "Chat"


def test_set_thread_model_rejects_provider_level_non_chat_string_model(tmp_path):
    models = {
        "providers": {
            "openai-images": {
                "api_base": "https://api.openai.com/v1/images/generations",
                "api_key_env": "OPENAI_API_KEY",
                "model_kind": "image_generation",
                "models": {"Image Backend": "gpt-image-1"},
            },
            "openai": {
                "api_base": "https://api.openai.com/v1/chat/completions",
                "api_key_env": "OPENAI_API_KEY",
                "models": {"Chat": "gpt-4o"},
            },
        },
    }
    models_path = tmp_path / "models.json"
    models_path.write_text(json.dumps(models), encoding="utf-8")
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="root", initial_model_key="Chat", models_path=str(models_path))

    with pytest.raises(ValueError, match="model_kind 'image_generation'"):
        ts.set_thread_model(db, tid, "Image Backend", models_path=str(models_path))

    assert ts.current_thread_model(db, tid) == "Chat"


def test_runner_aborts_when_thread_model_selection_is_rejected(tmp_path):
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="root")
    concrete = {
        "providers": {
            "openai": {
                "api_base": "https://api.openai.com/v1/chat/completions",
                "api_key_env": "OPENAI_API_KEY",
                "models": {
                    "Bad Chat": {"model_name": "gpt-4o"}
                },
            }
        }
    }
    ts.set_thread_model(db, tid, "Bad Chat", concrete_model_info=concrete, reason="test")
    ts.append_message(db, tid, "user", "hello")
    ts.create_snapshot(db, tid)

    class RejectingLLM:
        current_model_key = "Chat"

        def __init__(self):
            self.calls = 0

        def set_model_with_config(self, model_key, config):
            raise ValueError("Model is not usable for normal chat")

        async def astream_chat(self, messages, **kwargs):
            self.calls += 1
            yield {"type": "done", "message": {"role": "assistant", "content": "should not happen"}}

    llm = RejectingLLM()
    runner = ts.ThreadRunner(db, tid, llm=llm)

    assert asyncio.run(runner.run_once()) is True

    assert llm.calls == 0
    snapshot = ts.create_snapshot(db, tid)
    assert any(
        m.get("role") == "system" and "not usable for normal chat" in str(m.get("content"))
        for m in snapshot["messages"]
    )
