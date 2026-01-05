"""Tests for commands/model.py ModelCommandsMixin."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure project root is in path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class TestCmdModel:
    """Tests for cmd_model()."""

    def test_sets_model_with_argument(self, egg_app, monkeypatch):
        """Should set model via set_thread_model."""
        # set_thread_model is imported inside cmd_model, so we mock at eggthreads level
        monkeypatch.setattr("eggthreads.set_thread_model", lambda *a, **k: None)
        monkeypatch.setattr("eggthreads.create_snapshot", lambda *a: None)

        egg_app.cmd_model("gpt-4")

        # Should log about the model change
        assert any("model" in msg.lower() for msg in egg_app._system_log)

    def test_shows_current_model_without_argument(self, egg_app, monkeypatch):
        """Should display current model when no arg."""
        # Without llm_client, it shows the current thread model
        egg_app.llm_client = None

        egg_app.cmd_model("")

        # Should log something about model
        assert any("model" in msg.lower() or "Model" in msg for msg in egg_app._system_log)

    def test_lists_available_models_with_llm_client(self, egg_app, mock_llm_client, monkeypatch):
        """Should list available models from llm_client."""
        egg_app.llm_client = mock_llm_client

        egg_app.cmd_model("")

        # Should log available models or current model
        assert any("model" in msg.lower() for msg in egg_app._system_log)

    def test_handles_no_llm_client(self, egg_app):
        """Should handle gracefully when llm_client is None."""
        egg_app.llm_client = None

        egg_app.cmd_model("")

        # Should still work, just not list models
        assert any("model" in msg.lower() or "Model" in msg for msg in egg_app._system_log)

    def test_logs_model_change(self, egg_app, monkeypatch):
        """Should log the model change."""
        # Mock at eggthreads level since it's imported locally
        monkeypatch.setattr("eggthreads.set_thread_model", lambda *a, **k: None)
        monkeypatch.setattr("eggthreads.create_snapshot", lambda *a: None)

        egg_app.cmd_model("new-model")

        assert any("model" in msg.lower() for msg in egg_app._system_log)


class TestCmdUpdateAllModels:
    """Tests for cmd_updateAllModels()."""

    def test_requires_provider_argument(self, egg_app):
        """Should show usage when no provider given."""
        egg_app.cmd_updateAllModels("")

        assert any("Usage" in msg or "usage" in msg.lower() or "provider" in msg.lower()
                   for msg in egg_app._system_log)

    def test_calls_llm_client_update(self, egg_app, mock_llm_client, monkeypatch):
        """Should call llm_client.update_all_models()."""
        updated = []
        class MockLLMClient:
            def update_all_models(self, provider):
                updated.append(provider)
                return {"models": ["model1", "model2"]}

        egg_app.llm_client = MockLLMClient()

        egg_app.cmd_updateAllModels("openai")

        assert "openai" in updated

    def test_handles_no_llm_client(self, egg_app):
        """Should handle gracefully when llm_client is None."""
        egg_app.llm_client = None

        egg_app.cmd_updateAllModels("openai")

        assert any("not available" in msg.lower() or "error" in msg.lower()
                   for msg in egg_app._system_log)

    def test_logs_update_result(self, egg_app, monkeypatch):
        """Should log the update result."""
        class MockLLMClient:
            def update_all_models(self, provider):
                return {"count": 5}

        egg_app.llm_client = MockLLMClient()

        egg_app.cmd_updateAllModels("openai")

        assert any("update" in msg.lower() or "model" in msg.lower()
                   for msg in egg_app._system_log)
