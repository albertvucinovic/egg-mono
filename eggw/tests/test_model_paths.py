"""Tests for EggW model configuration path resolution."""
from __future__ import annotations

import json

from eggconfig import get_all_models_path, get_image_generation_models_path, get_models_path
from eggw.core import state


def test_resolve_model_paths_prefers_egg_cwd_models_json(tmp_path, monkeypatch):
    monkeypatch.delenv("EGG_MODELS_PATH", raising=False)
    monkeypatch.delenv("EGG_ALL_MODELS_PATH", raising=False)
    (tmp_path / "models.json").write_text(json.dumps({"providers": {}}), encoding="utf-8")

    models_path, all_models_path = state.resolve_model_paths(tmp_path)

    assert models_path == (tmp_path / "models.json").resolve()
    assert all_models_path == (tmp_path / "all-models.json").resolve()


def test_resolve_model_paths_uses_packaged_defaults_without_local_models(tmp_path, monkeypatch):
    monkeypatch.delenv("EGG_MODELS_PATH", raising=False)
    monkeypatch.delenv("EGG_ALL_MODELS_PATH", raising=False)

    models_path, all_models_path = state.resolve_model_paths(tmp_path)

    assert models_path == get_models_path().resolve()
    assert all_models_path == get_all_models_path().resolve()


def test_resolve_model_paths_honors_relative_env_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("EGG_MODELS_PATH", "config/my-models.json")
    monkeypatch.setenv("EGG_ALL_MODELS_PATH", "config/my-all-models.json")

    models_path, all_models_path = state.resolve_model_paths(tmp_path)

    assert models_path == (tmp_path / "config" / "my-models.json").resolve()
    assert all_models_path == (tmp_path / "config" / "my-all-models.json").resolve()


def test_resolve_image_generation_models_path_prefers_egg_cwd_file(tmp_path, monkeypatch):
    monkeypatch.delenv("EGG_IMAGE_GENERATION_MODELS_PATH", raising=False)
    (tmp_path / "image-generation-models.json").write_text(json.dumps({"models": {}}), encoding="utf-8")

    image_models_path = state.resolve_image_generation_models_path(tmp_path)

    assert image_models_path == (tmp_path / "image-generation-models.json").resolve()


def test_resolve_image_generation_models_path_uses_packaged_default_without_local_file(tmp_path, monkeypatch):
    monkeypatch.delenv("EGG_IMAGE_GENERATION_MODELS_PATH", raising=False)

    image_models_path = state.resolve_image_generation_models_path(tmp_path)

    assert image_models_path == get_image_generation_models_path().resolve()


def test_resolve_image_generation_models_path_honors_relative_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("EGG_IMAGE_GENERATION_MODELS_PATH", "config/image-models.json")

    image_models_path = state.resolve_image_generation_models_path(tmp_path)

    assert image_models_path == (tmp_path / "config" / "image-models.json").resolve()
