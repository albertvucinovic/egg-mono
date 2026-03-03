"""Tests for sandbox providers (srt, docker, bwrap)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch, Mock
import pytest

import eggthreads as _eggthreads_mod


@pytest.fixture
def eggthreads(monkeypatch, tmp_path):
    """Fixture providing eggthreads module with isolated environment."""
    monkeypatch.chdir(tmp_path)
    return _eggthreads_mod


def test_get_provider_names(eggthreads):
    """Test that provider names are correctly registered."""
    sandbox = eggthreads.sandbox
    names = sandbox.get_provider_names()
    assert isinstance(names, list)
    assert "srt" in names
    assert "docker" in names
    assert "bwrap" in names
    assert len(names) == 3


def test_provider_available(eggthreads):
    """Test provider availability checking."""
    sandbox = eggthreads.sandbox
    
    # Mock the underlying provider availability
    with patch.object(sandbox._PROVIDERS["srt"], "is_available", return_value=True):
        assert sandbox.provider_available("srt") is True
    
    with patch.object(sandbox._PROVIDERS["srt"], "is_available", return_value=False):
        assert sandbox.provider_available("srt") is False
    
    # Unknown provider should return False
    assert sandbox.provider_available("unknown") is False


def test_srt_provider_basic(eggthreads):
    """Test SRT provider basic functionality."""
    sandbox = eggthreads.sandbox
    provider = sandbox._PROVIDERS["srt"]
    assert provider.name == "srt"
    
    # Test is_available with mock
    with patch("shutil.which", return_value="/usr/bin/srt"):
        assert provider.is_available() is True
    
    with patch("shutil.which", return_value=None):
        assert provider.is_available() is False


def test_docker_provider_basic(eggthreads):
    """Test Docker provider basic functionality."""
    sandbox = eggthreads.sandbox
    provider = sandbox._PROVIDERS["docker"]
    assert provider.name == "docker"
    
    # Test is_available with mock
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock()
        mock_run.return_value.returncode = 0
        assert provider.is_available() is True
    
    with patch("subprocess.run", side_effect=Exception("docker not found")):
        assert provider.is_available() is False


def test_bwrap_provider_basic(eggthreads):
    """Test Bwrap provider basic functionality."""
    sandbox = eggthreads.sandbox
    provider = sandbox._PROVIDERS["bwrap"]
    assert provider.name == "bwrap"
    
    # Test is_available with mock
    with patch("shutil.which", return_value="/usr/bin/bwrap"):
        assert provider.is_available() is True
    
    with patch("shutil.which", return_value=None):
        assert provider.is_available() is False


def test_srt_provider_wrap_argv(eggthreads):
    """Test SRT provider argv wrapping."""
    sandbox = eggthreads.sandbox
    provider = sandbox._PROVIDERS["srt"]
    
    # Mock availability
    with patch.object(provider, "is_available", return_value=True):
        # Mock the effective config path generation
        with patch.object(sandbox, "_effective_config_path_from_settings") as mock_eff_path:
            mock_eff_path.return_value = Path("/tmp/test.json")
            
            # Mock shlex.join to avoid actual shell escaping issues
            with patch("shlex.join", return_value="echo hello"):
                argv = ["echo", "hello"]
                settings = {}
                wrapped = provider.wrap_argv(argv, settings)
                
                assert wrapped[0] == "srt"
                assert wrapped[1] == "--settings"
                assert wrapped[2] == "/tmp/test.json"
                assert wrapped[3] == "echo hello"
        
        # Test with working directory - mock the internal logic
        with patch.object(sandbox, "_effective_config_path_from_settings") as mock_eff_path:
            mock_eff_path.return_value = Path("/tmp/test2.json")
            with patch("shlex.join", return_value="ls -la"):
                argv = ["ls", "-la"]
                settings = {}
                # We need to mock Path.cwd() for the relative path calculation
                with patch("pathlib.Path.cwd", return_value=Path("/tmp")):
                    wrapped = provider.wrap_argv(argv, settings, working_dir=Path("/tmp/subdir"))
                    
                    assert wrapped[0] == "srt"
                    assert wrapped[1] == "--settings"
                    assert wrapped[2] == "/tmp/test2.json"
    
    # Test when not available - should return original argv
    with patch.object(provider, "is_available", return_value=False):
        argv = ["echo", "hello"]
        wrapped = provider.wrap_argv(argv, {})
        assert wrapped == argv


def test_docker_provider_wrap_argv(eggthreads):
    """Test Docker provider argv wrapping."""
    sandbox = eggthreads.sandbox
    provider = sandbox._PROVIDERS["docker"]
    
    with patch.object(provider, "is_available", return_value=True):
        argv = ["echo", "hello", "world"]
        settings = {}
        wrapped = provider.wrap_argv(argv, settings)
        
        # Basic docker command structure
        assert wrapped[0] == "docker"
        assert wrapped[1] == "run"
        assert wrapped[2] == "--rm"
        assert wrapped[3] == "--init"  # For proper signal forwarding
        assert wrapped[4] == "--user"
        #wrapped[5] is the id of the user, which we do not know so skipping
        assert wrapped[6] == "--network"
        assert wrapped[7] == "none"
        assert wrapped[8] == "-v"
        # Current directory mounted
        assert "-w" in wrapped
        #assert "python:3.12-slim" in wrapped # we no longer know which image is used depends on what exists
        assert wrapped[-3:] == ["echo", "hello", "world"]
    
    # Test with custom settings
    with patch.object(provider, "is_available", return_value=True):
        argv = ["python", "-c", "print(\'test\')"]
        settings = {
            "image": "alpine:latest",
            "network": "host",
            "workspace": "/app",
            "extra_args": ["--cap-drop", "ALL"],
            "extra_mounts": [
                {"src": "/tmp/data", "dst": "/data"}
            ]
        }
        wrapped = provider.wrap_argv(argv, settings, working_dir=Path("/home/user/project"))
        
        assert "docker" == wrapped[0]
        assert "run" == wrapped[1]
        assert "--rm" == wrapped[2]
        assert "--network" in wrapped
        assert "host" in wrapped
        assert "-v" in wrapped
        # Find the volume mount for working dir
        vol_idx = wrapped.index("-v")
        assert wrapped[vol_idx + 1] == "/home/user/project:/app"
        # Check for extra mount
        assert "-v" in wrapped[vol_idx + 2:]  # Another -v after the first
        assert "--cap-drop" in wrapped
        assert "ALL" in wrapped
        assert "-w" in wrapped
        assert "/app" in wrapped
        assert "alpine:latest" in wrapped
        assert wrapped[-3:] == ["python", "-c", "print(\'test\')"]
    
    # Test when not available
    with patch.object(provider, "is_available", return_value=False):
        argv = ["echo", "test"]
        wrapped = provider.wrap_argv(argv, {})
        assert wrapped == argv


def test_bwrap_provider_wrap_argv(eggthreads):
    """Test Bwrap provider argv wrapping."""
    sandbox = eggthreads.sandbox
    provider = sandbox._PROVIDERS["bwrap"]
    
    with patch.object(provider, "is_available", return_value=True):
        argv = ["ls", "-la"]
        settings = {}
        wrapped = provider.wrap_argv(argv, settings, working_dir=Path("/tmp/test"))
        
        assert wrapped[0] == "bwrap"
        assert "--ro-bind" in wrapped
        assert "--bind" in wrapped
        assert "/tmp/test" in wrapped
        assert "--dev" in wrapped
        assert "--proc" in wrapped
        assert "--unshare-net" in wrapped
        assert "--chdir" in wrapped
        assert "/tmp/test" in wrapped
        assert wrapped[-2:] == ["ls", "-la"]
    
    # Test when not available
    with patch.object(provider, "is_available", return_value=False):
        argv = ["echo", "test"]
        wrapped = provider.wrap_argv(argv, {})
        assert wrapped == argv


def test_wrap_argv_for_sandbox_with_settings_provider_selection(eggthreads):
    """Test provider selection in wrap_argv_for_sandbox_with_settings."""
    sandbox = eggthreads.sandbox
    
    # Test default provider (docker)
    with patch.object(sandbox._PROVIDERS["docker"], "is_available", return_value=True):
        with patch.object(sandbox._PROVIDERS["docker"], "wrap_argv") as mock_wrap:
            mock_wrap.return_value = ["docker", "run", "cmd"]
            argv = ["echo", "test"]
            settings = {}
            wrapped = sandbox.wrap_argv_for_sandbox_with_settings(
                argv, enabled=True, settings=settings
            )
            mock_wrap.assert_called_once()
            assert wrapped[0] == "docker"
    
    # Test explicit provider via settings
    with patch.object(sandbox._PROVIDERS["docker"], "is_available", return_value=True):
        with patch.object(sandbox._PROVIDERS["docker"], "wrap_argv") as mock_wrap:
            mock_wrap.return_value = ["docker", "run", "cmd"]
            argv = ["echo", "test"]
            settings = {"provider": "docker"}
            wrapped = sandbox.wrap_argv_for_sandbox_with_settings(
                argv, enabled=True, settings=settings
            )
            mock_wrap.assert_called_once()
            assert wrapped[0] == "docker"
    
    # Test explicit provider via parameter (overrides settings)
    with patch.object(sandbox._PROVIDERS["bwrap"], "is_available", return_value=True):
        with patch.object(sandbox._PROVIDERS["bwrap"], "wrap_argv") as mock_wrap:
            mock_wrap.return_value = ["bwrap", "cmd"]
            argv = ["echo", "test"]
            settings = {"provider": "docker"}  # Should be overridden
            wrapped = sandbox.wrap_argv_for_sandbox_with_settings(
                argv, enabled=True, settings=settings, provider="bwrap"
            )
            mock_wrap.assert_called_once()
            assert wrapped[0] == "bwrap"
    
    # Test unknown provider -> no sandbox
    argv = ["echo", "test"]
    settings = {"provider": "unknown"}
    wrapped = sandbox.wrap_argv_for_sandbox_with_settings(
        argv, enabled=True, settings=settings
    )
    assert wrapped == argv  # No sandbox applied
    
    # Test disabled sandbox
    with patch.object(sandbox._PROVIDERS["srt"], "is_available", return_value=True):
        argv = ["echo", "test"]
        wrapped = sandbox.wrap_argv_for_sandbox_with_settings(
            argv, enabled=False, settings={}
        )
        assert wrapped == argv  # No sandbox when disabled
def test_get_sandbox_status_includes_providers(eggthreads):
    """Test that get_sandbox_status includes provider availability."""
    sandbox = eggthreads.sandbox
    
    with patch.object(sandbox._PROVIDERS["srt"], "is_available", return_value=True):
        with patch.object(sandbox._PROVIDERS["docker"], "is_available", return_value=False):
            with patch.object(sandbox._PROVIDERS["bwrap"], "is_available", return_value=True):
                status = sandbox.get_sandbox_status()
                
                assert "providers" in status
                providers = status["providers"]
                assert isinstance(providers, dict)
                assert providers["srt"] is True
                assert providers["docker"] is False
                assert providers["bwrap"] is True


def test_thread_sandbox_config_with_provider(eggthreads, tmp_path):
    """Test that ThreadSandboxConfig includes provider field."""
    sandbox = eggthreads.sandbox
    
    # Create a DB using proper initialization
    db = eggthreads.ThreadsDB()
    db.init_schema()
    
    # Create a thread using the API
    thread_id = eggthreads.create_root_thread(db, name="test")
    eggthreads.append_message(db, thread_id, "system", "test")
    
    # Set sandbox config with docker provider
    sandbox.set_thread_sandbox_config(
        db, thread_id, enabled=True, provider="docker", reason="test"
    )
    
    # Get config
    config = sandbox.get_thread_sandbox_config(db, thread_id)
    assert config.enabled is True
    assert config.provider == "docker"
    assert isinstance(config.settings, dict)
    assert config.source == "default.json"
    
    # Test get_thread_sandbox_status includes provider
    status = sandbox.get_thread_sandbox_status(db, thread_id)
    assert "provider" in status
    assert status["provider"] == "docker"

def test_set_thread_sandbox_config_provider_inference(eggthreads, tmp_path):
    """Test that provider is inferred from settings if not specified."""
    sandbox = eggthreads.sandbox
    
    # Create a DB using proper initialization
    db = eggthreads.ThreadsDB()
    db.init_schema()
    
    # Create a thread
    thread_id = eggthreads.create_root_thread(db, name="test")
    eggthreads.append_message(db, thread_id, "system", "test")
    
    # Set config with provider in settings
    settings_with_provider = {
        "provider": "bwrap",
        "network": {"allowedDomains": []}
    }
    sandbox.set_thread_sandbox_config(
        db, thread_id, enabled=True, settings=settings_with_provider, reason="test"
    )
    
    # Get config - provider should be extracted from settings
    config = sandbox.get_thread_sandbox_config(db, thread_id)
    assert config.provider == "bwrap"
    
    # Set config without provider in settings - should default to "docker"
    settings_without_provider = {"network": {"allowedDomains": []}}
    sandbox.set_thread_sandbox_config(
        db, thread_id, enabled=True, settings=settings_without_provider, 
        provider=None, reason="test2"
    )
    
    config2 = sandbox.get_thread_sandbox_config(db, thread_id)
    assert config2.provider == "docker"

def test_working_dir_handling(eggthreads):
    """Test that working directory is properly handled by providers."""
    sandbox = eggthreads.sandbox
    
    # Test Docker provider working_dir
    provider = sandbox._PROVIDERS["docker"]
    
    with patch.object(provider, "is_available", return_value=True):
        argv = ["ls"]
        settings = {}
        
        wrapped = provider.wrap_argv(argv, settings, working_dir=Path("/custom/dir"))
        
        # Check that /custom/dir is mounted
        assert "-v" in wrapped
        vol_index = wrapped.index("-v")
        assert wrapped[vol_index + 1] == "/custom/dir:/workspace"
        
        # Check working directory is set
        assert "-w" in wrapped
        w_index = wrapped.index("-w")
        assert wrapped[w_index + 1] == "/workspace"
    
    # Test Bwrap provider working_dir
    provider = sandbox._PROVIDERS["bwrap"]
    
    with patch.object(provider, "is_available", return_value=True):
        argv = ["ls"]
        settings = {}
        
        wrapped = provider.wrap_argv(argv, settings, working_dir=Path("/custom/dir"))
        
        # Check bind mount
        assert "--bind" in wrapped
        bind_index = wrapped.index("--bind")
        assert wrapped[bind_index + 1] == "/custom/dir"
        assert wrapped[bind_index + 2] == "/custom/dir"
        
        # Check chdir
        assert "--chdir" in wrapped
        chdir_index = wrapped.index("--chdir")
        assert wrapped[chdir_index + 1] == "/custom/dir"



def test_augment_with_protections_adds_egg_protection(eggthreads):
    """Test that _augment_with_protections adds .egg to denyWrite and denyRead."""
    sandbox = eggthreads.sandbox
    # Create a minimal config
    cfg = {}
    augmented = sandbox._augment_with_protections(cfg)
    # Should have filesystem dict
    assert "filesystem" in augmented
    fs = augmented["filesystem"]
    # Should have denyWrite list containing .egg directory
    deny_write = fs.get("denyWrite")
    assert isinstance(deny_write, list)
    # Compute expected .egg path (parent of sandbox dir)
    sandbox_dir = sandbox._ensure_sandbox_dir()
    egg_dir = sandbox_dir.parent
    assert str(egg_dir) in deny_write
    # Also should have denyRead list containing .egg
    deny_read = fs.get("denyRead")
    assert isinstance(deny_read, list)
    assert str(egg_dir) in deny_read
    # Sandbox directory and default.json also protected
    assert str(sandbox_dir) in deny_write
    assert str(sandbox_dir / "default.json") in deny_write


def test_docker_provider_protects_egg_dir(eggthreads, tmp_path):
    """Test that Docker provider adds read-only mount for .egg directory."""
    sandbox = eggthreads.sandbox
    provider = sandbox._PROVIDERS["docker"]
    
    with patch.object(provider, "is_available", return_value=True):
        argv = ["ls"]
        settings = {}
        # When working dir is same as CWD, .egg is inside
        wrapped = provider.wrap_argv(argv, settings, working_dir=tmp_path)
        # Should have a -v mount for .egg as read-only
        # Find all -v occurrences
        vol_indices = [i for i, arg in enumerate(wrapped) if arg == "-v"]
        egg_dir = tmp_path / ".egg"
        # Create .egg directory for relative path detection
        egg_dir.mkdir(exist_ok=True)
        found = False
        for idx in vol_indices:
            mount_spec = wrapped[idx + 1]
            if mount_spec.startswith(str(egg_dir) + ":"):
                # Should contain :ro (may be at end or middle)
                if ":ro" in mount_spec:
                    found = True
                break
        assert found, f"Expected read-only mount for .egg in {wrapped}"
        
        # When working dir is elsewhere (outside .egg), no extra mount needed
        other_dir = tmp_path / "other"
        other_dir.mkdir()
        wrapped2 = provider.wrap_argv(argv, settings, working_dir=other_dir)
        # Should not have .egg mount because .egg not inside other_dir
        egg_inside = False
        for idx in range(len(wrapped2)):
            if wrapped2[idx] == "-v" and idx+1 < len(wrapped2):
                if wrapped2[idx+1].startswith(str(egg_dir) + ":"):
                    egg_inside = True
        assert not egg_inside, f".egg mount should not appear when .egg not inside working dir: {wrapped2}"
def test_bwrap_provider_protects_egg_dir(eggthreads, tmp_path):
    """Test that Bwrap provider adds --ro-bind for .egg directory."""
    sandbox = eggthreads.sandbox
    provider = sandbox._PROVIDERS["bwrap"]
    
    with patch.object(provider, "is_available", return_value=True):
        argv = ["ls"]
        settings = {}
        # Working dir same as CWD, .egg inside
        wrapped = provider.wrap_argv(argv, settings, working_dir=tmp_path)
        # Should have --ro-bind for .egg
        egg_dir = tmp_path / ".egg"
        egg_dir.mkdir(exist_ok=True)
        found = False
        for i, arg in enumerate(wrapped):
            if arg == "--ro-bind" and i+2 < len(wrapped):
                src = wrapped[i+1]
                dst = wrapped[i+2]
                if src == str(egg_dir) and dst == str(egg_dir):
                    found = True
        assert found, f"Expected --ro-bind for .egg in {wrapped}"
        
        # When working dir elsewhere, .egg not inside, root ro-bind already protects
        other_dir = tmp_path / "other"
        other_dir.mkdir()
        wrapped2 = provider.wrap_argv(argv, settings, working_dir=other_dir)
        # Should not have extra --ro-bind for .egg (since root ro-bind covers)
        extra_ro_bind = False
        for i, arg in enumerate(wrapped2):
            if arg == "--ro-bind" and i+2 < len(wrapped2):
                src = wrapped2[i+1]
                if src == str(egg_dir):
                    extra_ro_bind = True
        # Actually we still add ro-bind for .egg even if not inside? Let's check implementation.
        # The implementation adds ro-bind if egg_dir is inside wd, else not.
        # So we should assert not extra_ro_bind.
        assert not extra_ro_bind, f"Unexpected --ro-bind for .egg when not inside working dir: {wrapped2}"


def test_srt_provider_protects_egg_dir(eggthreads):
    """Test that SRT provider settings include .egg protection via augmentation."""
    sandbox = eggthreads.sandbox
    provider = sandbox._PROVIDERS["srt"]
    
    with patch.object(provider, "is_available", return_value=True):
        argv = ["echo", "test"]
        settings = {}
        # Mock _effective_config_path_from_settings to capture augmented config
        original = sandbox._effective_config_path_from_settings
        captured_config = None
        def mock_eff(s):
            nonlocal captured_config
            # Call original to get augmented config
            path = original(s)
            captured_config = sandbox._augment_with_protections(s)
            return path
        sandbox._effective_config_path_from_settings = mock_eff
        try:
            provider.wrap_argv(argv, settings, working_dir=Path.cwd())
            # Verify captured_config includes .egg protection
            assert captured_config is not None
            fs = captured_config.get("filesystem", {})
            deny_write = fs.get("denyWrite", [])
            deny_read = fs.get("denyRead", [])
            egg_dir = sandbox._ensure_sandbox_dir().parent
            assert str(egg_dir) in deny_write
            assert str(egg_dir) in deny_read
        finally:
            sandbox._effective_config_path_from_settings = original
def test_integration_thread_with_docker_provider(eggthreads, tmp_path):
    """Integration test: set docker provider and verify command wrapping."""
    sandbox = eggthreads.sandbox
    
    # Create a DB
    db = eggthreads.ThreadsDB()
    db.init_schema()
    
    # Create a thread
    thread_id = eggthreads.create_root_thread(db, name="test")
    eggthreads.append_message(db, thread_id, "system", "test")
    
    # Mock docker provider as available
    with patch.object(sandbox._PROVIDERS["docker"], "is_available", return_value=True):
        # Set sandbox config with docker provider
        sandbox.set_thread_sandbox_config(
            db, thread_id, enabled=True, provider="docker", reason="test"
        )
        
        # Get config
        config = sandbox.get_thread_sandbox_config(db, thread_id)
        assert config.provider == "docker"
        
        # Simulate what runner.py would do: wrap argv with thread config
        from unittest.mock import patch as mock_patch
        with mock_patch.object(sandbox._PROVIDERS["docker"], "wrap_argv") as mock_wrap:
            mock_wrap.return_value = ["docker", "run", "echo", "hello"]
            argv = ["echo", "hello"]
            wrapped = sandbox.wrap_argv_for_sandbox_with_settings(
                argv,
                enabled=config.enabled,
                settings=config.settings,
                provider=config.provider,
                working_dir=tmp_path
            )
            # Should have called docker provider
            mock_wrap.assert_called_once()
            assert wrapped[0] == "docker"
    
    # Test that when docker is unavailable, it falls back to no sandbox
    with patch.object(sandbox._PROVIDERS["docker"], "is_available", return_value=False):
        # Re-get config (provider still docker)
        config = sandbox.get_thread_sandbox_config(db, thread_id)
        assert config.provider == "docker"
        
        # Wrap should return original argv because provider unavailable
        argv = ["echo", "test"]
        wrapped = sandbox.wrap_argv_for_sandbox_with_settings(
            argv,
            enabled=config.enabled,
            settings=config.settings,
            provider=config.provider,
        )
        assert wrapped == argv  # No sandbox applied


def test_default_config_includes_provider(eggthreads):
    """Test that the default config dict includes provider field."""
    sandbox = eggthreads.sandbox
    default_dict = sandbox._default_config_dict()
    assert "provider" in default_dict
    assert default_dict["provider"] == "docker"
    # Ensure other expected keys are present
    assert "network" in default_dict
    assert "filesystem" in default_dict


@pytest.mark.skipif(
    os.environ.get("EGG_SKIP_DOCKER_TESTS") == "1",
    reason="Docker tests disabled via EGG_SKIP_DOCKER_TESTS=1",
)
def test_real_docker_provider_if_available(eggthreads, tmp_path):
    """Test real docker provider if docker is available on system."""
    sandbox = eggthreads.sandbox
    provider = sandbox._PROVIDERS["docker"]
    
    if not provider.is_available():
        pytest.skip("Docker not available on system")
    
    # Basic test: ensure wrap_argv produces valid docker command
    argv = ["echo", "hello"]
    settings = {"image": "alpine:latest"}
    wrapped = provider.wrap_argv(argv, settings, working_dir=tmp_path)
    
    assert wrapped[0] == "docker"
    assert "run" in wrapped
    assert "--rm" in wrapped
    assert "alpine:latest" in wrapped
    assert wrapped[-2:] == ["echo", "hello"]
    
    # Test with default settings
    wrapped_default = provider.wrap_argv(["ls"], {}, working_dir=tmp_path)
    assert wrapped_default[0] == "docker"
    #assert "python:3.12-slim" in wrapped_default  # default image


@pytest.mark.skipif(
    os.environ.get("EGG_SKIP_BWRAP_TESTS") == "1",
    reason="Bwrap tests disabled via EGG_SKIP_BWRAP_TESTS=1",
)
def test_real_bwrap_provider_if_available(eggthreads, tmp_path):
    """Test real bwrap provider if bwrap is available on system."""
    sandbox = eggthreads.sandbox
    provider = sandbox._PROVIDERS["bwrap"]
    
    if not provider.is_available():
        pytest.skip("Bwrap not available on system")
    
    argv = ["echo", "test"]
    wrapped = provider.wrap_argv(argv, {}, working_dir=tmp_path)
    
    assert wrapped[0] == "bwrap"
    assert "--ro-bind" in wrapped
    assert "--bind" in wrapped
    assert "--unshare-net" in wrapped
    assert wrapped[-2:] == ["echo", "test"]


def test_docker_provider_with_subthreads_and_cwd(eggthreads, tmp_path):
    """Test docker provider with subthreads and custom working directories."""
    sandbox = eggthreads.sandbox
    
    # Create a DB
    db = eggthreads.ThreadsDB()
    db.init_schema()
    
    # Create root thread with docker provider
    root = eggthreads.create_root_thread(db, name="root")
    eggthreads.append_message(db, root, "system", "test")
    
    # Set sandbox config with docker provider
    sandbox.set_thread_sandbox_config(
        db, root, enabled=True, provider="docker", reason="test"
    )
    
    # Create child thread with custom working directory
    child = eggthreads.create_child_thread(db, root, name="child")
    custom_dir = tmp_path / "custom_cwd"
    custom_dir.mkdir()
    eggthreads.set_thread_working_directory(db, child, str(custom_dir))
    
    # Get config for child (should inherit docker provider)
    config = sandbox.get_thread_sandbox_config(db, child)
    assert config.provider == "docker"
    
    # Mock docker provider to verify working directory
    with patch.object(sandbox._PROVIDERS["docker"], "is_available", return_value=True):
        with patch.object(sandbox._PROVIDERS["docker"], "wrap_argv") as mock_wrap:
            mock_wrap.return_value = ["docker", "run", "cmd"]
            
            # Simulate what runner would do: get thread working directory
            child_cwd = eggthreads.get_thread_working_directory(db, child)
            # Should be the custom directory
            assert str(child_cwd) == str(custom_dir)
            
            # Simulate wrapping argv
            argv = ["echo", "hello"]
            wrapped = sandbox.wrap_argv_for_sandbox_with_settings(
                argv,
                enabled=config.enabled,
                settings=config.settings,
                provider=config.provider,
                working_dir=child_cwd
            )
            
            # Verify wrap_argv called with correct working_dir
            mock_wrap.assert_called_once()
            call_args = mock_wrap.call_args
            # Verify wrap_argv called with correct working_dir
            mock_wrap.assert_called_once()
            call_args = mock_wrap.call_args
            # The call includes argv, settings, and optionally working_dir as positional args
            assert len(call_args[0]) >= 2  # argv, settings
            # working_dir may be third positional argument
            if len(call_args[0]) >= 3:
                working_dir_arg = call_args[0][2]
            else:
                # Or as keyword argument
                assert "working_dir" in call_args[1]
                working_dir_arg = call_args[1]["working_dir"]
            # Should be a Path pointing to custom_dir
            from pathlib import Path
            assert isinstance(working_dir_arg, Path)
            assert str(working_dir_arg) == str(custom_dir)
            # Verify wrap_argv called with correct working_dir
            mock_wrap.assert_called_once()
            call_args = mock_wrap.call_args
            # The call includes argv, settings, and optionally working_dir as positional args
            assert len(call_args[0]) >= 2  # argv, settings
            # working_dir may be third positional argument
            if len(call_args[0]) >= 3:
                working_dir_arg = call_args[0][2]
            else:
                # Or as keyword argument
                assert "working_dir" in call_args[1]
                working_dir_arg = call_args[1]["working_dir"]
            # Should be a Path pointing to custom_dir
            from pathlib import Path
            assert isinstance(working_dir_arg, Path)
            assert str(working_dir_arg) == str(custom_dir)
            # Verify wrap_argv called with correct working_dir
            mock_wrap.assert_called_once()
            call_args = mock_wrap.call_args
            # The call includes argv, settings, and optionally working_dir as positional args
            assert len(call_args[0]) >= 2  # argv, settings
            # working_dir may be third positional argument
            if len(call_args[0]) >= 3:
                working_dir_arg = call_args[0][2]
            else:
                # Or as keyword argument
                assert "working_dir" in call_args[1]
                working_dir_arg = call_args[1]["working_dir"]
            # Should be a Path pointing to custom_dir
            from pathlib import Path
            assert isinstance(working_dir_arg, Path)
            assert str(working_dir_arg) == str(custom_dir)
            # Verify wrap_argv called with correct working_dir
            mock_wrap.assert_called_once()
            call_args = mock_wrap.call_args
            # The call includes argv, settings, and optionally working_dir as positional args
            assert len(call_args[0]) >= 2  # argv, settings
            # working_dir may be third positional argument
            if len(call_args[0]) >= 3:
                working_dir_arg = call_args[0][2]
            else:
                # Or as keyword argument
                assert "working_dir" in call_args[1]
                working_dir_arg = call_args[1]["working_dir"]
            # Should be a Path pointing to custom_dir
            from pathlib import Path
            assert isinstance(working_dir_arg, Path)
            assert str(working_dir_arg) == str(custom_dir)
            # Verify wrap_argv called with correct working_dir
            mock_wrap.assert_called_once()
            call_args = mock_wrap.call_args
            # The call includes argv, settings, and optionally working_dir as positional args
            assert len(call_args[0]) >= 2  # argv, settings
            # working_dir may be third positional argument
            if len(call_args[0]) >= 3:
                working_dir_arg = call_args[0][2]
            else:
                # Or as keyword argument
                assert "working_dir" in call_args[1]
                working_dir_arg = call_args[1]["working_dir"]
            # Should be a Path pointing to custom_dir
            from pathlib import Path
            assert isinstance(working_dir_arg, Path)
            assert str(working_dir_arg) == str(custom_dir)
            # Verify wrap_argv called with correct working_dir
            mock_wrap.assert_called_once()
            call_args = mock_wrap.call_args
            # The call includes argv, settings, and optionally working_dir as positional args
            assert len(call_args[0]) >= 2  # argv, settings
            # working_dir may be third positional argument
            if len(call_args[0]) >= 3:
                working_dir_arg = call_args[0][2]
            else:
                # Or as keyword argument
                assert "working_dir" in call_args[1]
                working_dir_arg = call_args[1]["working_dir"]
            # Should be a Path pointing to custom_dir
            from pathlib import Path
            assert isinstance(working_dir_arg, Path)
            assert str(working_dir_arg) == str(custom_dir)
            # Verify wrap_argv called with correct working_dir
            mock_wrap.assert_called_once()
            call_args = mock_wrap.call_args
            # The call includes argv, settings, and optionally working_dir as positional args
            assert len(call_args[0]) >= 2  # argv, settings
            # working_dir may be third positional argument
            if len(call_args[0]) >= 3:
                working_dir_arg = call_args[0][2]
            else:
                # Or as keyword argument
                assert "working_dir" in call_args[1]
                working_dir_arg = call_args[1]["working_dir"]
            # Should be a Path pointing to custom_dir
            from pathlib import Path
            assert isinstance(working_dir_arg, Path)
            assert str(working_dir_arg) == str(custom_dir)
            # Verify wrap_argv called with correct working_dir
            mock_wrap.assert_called_once()
            call_args = mock_wrap.call_args
            # The call includes argv, settings, and optionally working_dir as positional args
            assert len(call_args[0]) >= 2  # argv, settings
            # working_dir may be third positional argument
            if len(call_args[0]) >= 3:
                working_dir_arg = call_args[0][2]
            else:
                # Or as keyword argument
                assert "working_dir" in call_args[1]
                working_dir_arg = call_args[1]["working_dir"]
            # Should be a Path pointing to custom_dir
            from pathlib import Path
            assert isinstance(working_dir_arg, Path)
            assert str(working_dir_arg) == str(custom_dir)
            # Verify wrap_argv called with correct working_dir
            mock_wrap.assert_called_once()
            call_args = mock_wrap.call_args
            # The call includes argv, settings, and optionally working_dir as positional args
            assert len(call_args[0]) >= 2  # argv, settings
            # working_dir may be third positional argument
            if len(call_args[0]) >= 3:
                working_dir_arg = call_args[0][2]
            else:
                # Or as keyword argument
                assert "working_dir" in call_args[1]
                working_dir_arg = call_args[1]["working_dir"]
            # Should be a Path pointing to custom_dir
            from pathlib import Path
            assert isinstance(working_dir_arg, Path)
            assert str(working_dir_arg) == str(custom_dir)
            # Verify wrap_argv called with correct working_dir
            mock_wrap.assert_called_once()
            call_args = mock_wrap.call_args
            # The call includes argv, settings, and optionally working_dir as positional args
            assert len(call_args[0]) >= 2  # argv, settings
            # working_dir may be third positional argument
            if len(call_args[0]) >= 3:
                working_dir_arg = call_args[0][2]
            else:
                # Or as keyword argument
                assert "working_dir" in call_args[1]
                working_dir_arg = call_args[1]["working_dir"]
            # Should be a Path pointing to custom_dir
            from pathlib import Path
            assert isinstance(working_dir_arg, Path)
            assert str(working_dir_arg) == str(custom_dir)
            # Verify wrap_argv called with correct working_dir
            mock_wrap.assert_called_once()
            call_args = mock_wrap.call_args
            # The call includes argv, settings, and optionally working_dir as positional args
            assert len(call_args[0]) >= 2  # argv, settings
            # working_dir may be third positional argument
            if len(call_args[0]) >= 3:
                working_dir_arg = call_args[0][2]
            else:
                # Or as keyword argument
                assert "working_dir" in call_args[1]
                working_dir_arg = call_args[1]["working_dir"]
            # Should be a Path pointing to custom_dir
            from pathlib import Path
            assert isinstance(working_dir_arg, Path)
            assert str(working_dir_arg) == str(custom_dir)

def test_docker_provider_mounts_correct_directory(eggthreads, tmp_path):
    """Test that docker provider correctly mounts working directory."""
    sandbox = eggthreads.sandbox
    provider = sandbox._PROVIDERS["docker"]
    
    with patch.object(provider, "is_available", return_value=True):
        # Test with root directory
        argv = ["ls"]
        settings = {}
        wrapped = provider.wrap_argv(argv, settings, working_dir=tmp_path)
        
        # Check mount
        assert "-v" in wrapped
        vol_idx = wrapped.index("-v")
        assert wrapped[vol_idx + 1] == f"{tmp_path}:/workspace"
        
        # Test with subdirectory
        subdir = tmp_path / "sub" / "deep"
        subdir.mkdir(parents=True)
        wrapped2 = provider.wrap_argv(argv, settings, working_dir=subdir)
        vol_idx2 = wrapped2.index("-v")
        assert wrapped2[vol_idx2 + 1] == f"{subdir}:/workspace"
        
        # Test with custom workspace in settings
        settings_custom = {"workspace": "/app"}
        wrapped3 = provider.wrap_argv(argv, settings_custom, working_dir=tmp_path)
        vol_idx3 = wrapped3.index("-v")
        assert wrapped3[vol_idx3 + 1] == f"{tmp_path}:/app"
        # Check -w uses custom workspace
        w_idx = wrapped3.index("-w")
        assert wrapped3[w_idx + 1] == "/app"
