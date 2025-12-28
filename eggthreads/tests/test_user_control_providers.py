"""Tests for user sandbox control across all providers."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock
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


def test_user_control_with_docker_provider(eggthreads, tmp_path):
    """Test user control flag works with docker provider settings."""
    db = eggthreads.ThreadsDB()
    db.init_schema()
    root = eggthreads.create_root_thread(db, name="root")
    eggthreads.append_message(db, root, "system", "test")
    
    # Docker config with user control disabled
    config = {
        "provider": "docker",
        "image": "python:3.12-slim",
        "network": "none",
        "workspace": "/workspace",
        "extra_mounts": [],
        "extra_args": [],
        "user_control_enabled": False
    }
    
    # Set config via settings dict
    eggthreads.set_thread_sandbox_config(
        db, root, enabled=True, settings=config, reason="test"
    )
    
    # Verify user control disabled
    assert eggthreads.is_user_sandbox_control_enabled(db, root) is False
    
    # Verify config contains provider and user_control_enabled
    cfg = eggthreads.get_thread_sandbox_config(db, root)
    assert cfg.provider == "docker"
    assert cfg.user_control_enabled is False
    
    # Verify status includes provider
    status = eggthreads.get_thread_sandbox_status(db, root)
    assert status["provider"] == "docker"
    
    # Now enable via API
    eggthreads.enable_user_sandbox_control(db, root, reason="enable")
    assert eggthreads.is_user_sandbox_control_enabled(db, root) is True
    
    # Config should still have docker provider
    cfg = eggthreads.get_thread_sandbox_config(db, root)
    assert cfg.provider == "docker"
    assert cfg.user_control_enabled is True


def test_user_control_with_srt_provider(eggthreads, tmp_path):
    """Test user control flag works with SRT provider settings."""
    db = eggthreads.ThreadsDB()
    db.init_schema()
    root = eggthreads.create_root_thread(db, name="root")
    eggthreads.append_message(db, root, "system", "test")
    
    # SRT config with user control disabled
    config = {
        "provider": "srt",
        "filesystem": {
            "allowWrite": ["."],
            "denyWrite": [".egg"]
        },
        "network": {
            "allowedDomains": []
        },
        "user_control_enabled": False
    }
    
    eggthreads.set_thread_sandbox_config(
        db, root, enabled=True, settings=config, reason="test"
    )
    
    assert eggthreads.is_user_sandbox_control_enabled(db, root) is False
    cfg = eggthreads.get_thread_sandbox_config(db, root)
    assert cfg.provider == "srt"
    assert cfg.user_control_enabled is False
    
    # Disable via API (should stay disabled)
    eggthreads.disable_user_sandbox_control(db, root, reason="still disabled")
    assert eggthreads.is_user_sandbox_control_enabled(db, root) is False
    
    # Enable via API
    eggthreads.enable_user_sandbox_control(db, root, reason="enable")
    assert eggthreads.is_user_sandbox_control_enabled(db, root) is True
    cfg = eggthreads.get_thread_sandbox_config(db, root)
    assert cfg.provider == "srt"
    assert cfg.user_control_enabled is True


def test_user_control_with_bwrap_provider(eggthreads, tmp_path):
    """Test user control flag works with bwrap provider settings."""
    db = eggthreads.ThreadsDB()
    db.init_schema()
    root = eggthreads.create_root_thread(db, name="root")
    eggthreads.append_message(db, root, "system", "test")
    
    # Bwrap config with user control disabled
    config = {
        "provider": "bwrap",
        "user_control_enabled": False
    }
    
    eggthreads.set_thread_sandbox_config(
        db, root, enabled=True, settings=config, reason="test"
    )
    
    assert eggthreads.is_user_sandbox_control_enabled(db, root) is False
    cfg = eggthreads.get_thread_sandbox_config(db, root)
    assert cfg.provider == "bwrap"
    assert cfg.user_control_enabled is False
    
    # Toggle via disable/enable
    eggthreads.enable_user_sandbox_control(db, root, reason="enable")
    assert eggthreads.is_user_sandbox_control_enabled(db, root) is True
    eggthreads.disable_user_sandbox_control(db, root, reason="disable again")
    assert eggthreads.is_user_sandbox_control_enabled(db, root) is False


def test_user_control_inheritance_across_providers(eggthreads, tmp_path):
    """Test that user control flag is inherited regardless of provider."""
    db = eggthreads.ThreadsDB()
    db.init_schema()
    root = eggthreads.create_root_thread(db, name="root")
    eggthreads.append_message(db, root, "system", "test")
    
    # Set docker provider with user control disabled
    config = {
        "provider": "docker",
        "user_control_enabled": False
    }
    eggthreads.set_thread_sandbox_config(
        db, root, enabled=True, settings=config, reason="test"
    )
    
    # Create child thread
    child = eggthreads.create_child_thread(db, root, name="child")
    eggthreads.append_message(db, child, "system", "test")
    
    # Child should inherit docker provider and disabled user control
    cfg_child = eggthreads.get_thread_sandbox_config(db, child)
    assert cfg_child.provider == "docker"
    assert cfg_child.user_control_enabled is False
    assert eggthreads.is_user_sandbox_control_enabled(db, child) is False
    
    # Now change parent provider to srt, keep user control disabled
    config2 = {
        "provider": "srt",
        "filesystem": {"allowWrite": ["."]},
        "user_control_enabled": False
    }
    eggthreads.set_thread_sandbox_config(
        db, root, enabled=True, settings=config2, reason="switch"
    )
    
    # Child still inherits (no explicit config), but should reflect new provider
    cfg_child2 = eggthreads.get_thread_sandbox_config(db, child)
    assert cfg_child2.provider == "srt"
    assert cfg_child2.user_control_enabled is False
    
    # Enable user control on parent
    eggthreads.enable_user_sandbox_control(db, root, reason="enable")
    cfg_child3 = eggthreads.get_thread_sandbox_config(db, child)
    assert cfg_child3.user_control_enabled is True
    assert eggthreads.is_user_sandbox_control_enabled(db, child) is True


def test_user_control_with_config_file(eggthreads, tmp_path):
    """Test user control via config file works across providers."""
    db = eggthreads.ThreadsDB()
    db.init_schema()
    root = eggthreads.create_root_thread(db, name="root")
    eggthreads.append_message(db, root, "system", "test")
    
    # Create config file for docker with user control disabled
    config = {
        "provider": "docker",
        "image": "alpine:latest",
        "network": "none",
        "user_control_enabled": False
    }
    
    sandbox_dir = Path.cwd() / ".egg" / "sandbox"
    sandbox_dir.mkdir(parents=True, exist_ok=True)
    config_file = sandbox_dir / "docker_disabled.json"
    config_file.write_text(json.dumps(config), encoding="utf-8")
    
    # Apply via config_name
    eggthreads.set_thread_sandbox_config(
        db, root, enabled=True, config_name="docker_disabled.json", reason="file"
    )
    
    assert eggthreads.is_user_sandbox_control_enabled(db, root) is False
    cfg = eggthreads.get_thread_sandbox_config(db, root)
    assert cfg.provider == "docker"
    assert cfg.user_control_enabled is False
    
    # Create srt config file with user control enabled
    config2 = {
        "provider": "srt",
        "filesystem": {"allowWrite": ["."]},
        "user_control_enabled": True
    }
    config_file2 = sandbox_dir / "srt_enabled.json"
    config_file2.write_text(json.dumps(config2), encoding="utf-8")
    
    eggthreads.set_thread_sandbox_config(
        db, root, enabled=True, config_name="srt_enabled.json", reason="switch"
    )
    
    assert eggthreads.is_user_sandbox_control_enabled(db, root) is True
    cfg = eggthreads.get_thread_sandbox_config(db, root)
    assert cfg.provider == "srt"


def test_user_control_preserves_provider_settings(eggthreads, tmp_path):
    """Test that enable/disable_user_sandbox_control preserves provider settings."""
    db = eggthreads.ThreadsDB()
    db.init_schema()
    root = eggthreads.create_root_thread(db, name="root")
    eggthreads.append_message(db, root, "system", "test")
    
    # Start with docker config
    config = {
        "provider": "docker",
        "image": "alpine:latest",
        "network": "host",
        "extra_args": ["--cap-drop", "ALL"],
        "user_control_enabled": True
    }
    eggthreads.set_thread_sandbox_config(
        db, root, enabled=True, settings=config, reason="initial"
    )
    
    # Disable user control
    eggthreads.disable_user_sandbox_control(db, root, reason="disable")
    
    # Verify provider settings preserved
    cfg = eggthreads.get_thread_sandbox_config(db, root)
    assert cfg.provider == "docker"
    assert cfg.user_control_enabled is False
    # Check settings preserved
    settings = cfg.settings
    assert settings.get("image") == "alpine:latest"
    assert settings.get("network") == "host"
    assert settings.get("extra_args") == ["--cap-drop", "ALL"]
    
    # Enable user control
    eggthreads.enable_user_sandbox_control(db, root, reason="enable")
    
    cfg = eggthreads.get_thread_sandbox_config(db, root)
    assert cfg.provider == "docker"
    assert cfg.user_control_enabled is True
    settings = cfg.settings
    assert settings.get("image") == "alpine:latest"
    assert settings.get("network") == "host"


def test_user_control_with_provider_availability(eggthreads, tmp_path):
    """Test user control works independently of provider availability."""
    db = eggthreads.ThreadsDB()
    db.init_schema()
    root = eggthreads.create_root_thread(db, name="root")
    eggthreads.append_message(db, root, "system", "test")
    
    # Set docker provider with user control disabled
    config = {
        "provider": "docker",
        "user_control_enabled": False
    }
    eggthreads.set_thread_sandbox_config(
        db, root, enabled=True, settings=config, reason="test"
    )
    
    # Mock docker provider unavailable
    with patch.object(eggthreads.sandbox._PROVIDERS["docker"], "is_available", return_value=False):
        status = eggthreads.get_thread_sandbox_status(db, root)
        assert status["provider"] == "docker"
        assert status["available"] is False
        assert status["effective"] is False  # because unavailable
        # User control should still be disabled regardless of availability
        assert eggthreads.is_user_sandbox_control_enabled(db, root) is False
    
    # Switch to srt provider with user control enabled
    config2 = {
        "provider": "srt",
        "user_control_enabled": True
    }
    eggthreads.set_thread_sandbox_config(
        db, root, enabled=True, settings=config2, reason="switch"
    )
    
    # Mock srt provider available
    with patch.object(eggthreads.sandbox._PROVIDERS["srt"], "is_available", return_value=True):
        status = eggthreads.get_thread_sandbox_status(db, root)
        assert status["provider"] == "srt"
        assert status["available"] is True
        assert eggthreads.is_user_sandbox_control_enabled(db, root) is True


def test_user_control_default_true(eggthreads, tmp_path):
    """Test that user control defaults to True when no config present."""
    db = eggthreads.ThreadsDB()
    db.init_schema()
    root = eggthreads.create_root_thread(db, name="root")
    eggthreads.append_message(db, root, "system", "test")
    
    # No sandbox config at all
    assert eggthreads.is_user_sandbox_control_enabled(db, root) is True
    
    # Set config without user_control_enabled field (should default True)
    config = {
        "provider": "docker"
    }
    eggthreads.set_thread_sandbox_config(
        db, root, enabled=True, settings=config, reason="test"
    )
    assert eggthreads.is_user_sandbox_control_enabled(db, root) is True
    
    # Explicitly set to False
    config["user_control_enabled"] = False
    eggthreads.set_thread_sandbox_config(
        db, root, enabled=True, settings=config, reason="update"
    )
    assert eggthreads.is_user_sandbox_control_enabled(db, root) is False


if __name__ == '__main__':
    pytest.main([__file__])
