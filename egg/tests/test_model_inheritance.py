"""Test model inheritance in egg app.

Verifies that child threads automatically inherit the parent's model
configuration (including concrete_model_info) when created via
create_child_thread or spawn_agent tools.
"""

from __future__ import annotations

import json

import pytest


def test_child_thread_inherits_model(tmp_path, monkeypatch):
    """Test that create_child_thread automatically inherits parent's model."""
    monkeypatch.chdir(tmp_path)

    from eggthreads import (
        ThreadsDB, create_root_thread, create_child_thread,
        set_thread_model, current_thread_model, current_thread_model_info
    )

    db = ThreadsDB()
    db.init_schema()

    # Create parent thread
    parent = create_root_thread(db, name="Parent")

    # Set model on parent with concrete_model_info
    concrete = {
        "providers": {
            "openai": {
                "api_base": "https://api.openai.com/v1/chat/completions",
                "api_key_env": "OPENAI_API_KEY",
                "models": {
                    "GPT-4": {
                        "model_name": "gpt-4",
                        "max_tokens": 8192,
                        "cost": {"input_tokens": 0.03, "output_tokens": 0.06}
                    }
                }
            }
        }
    }
    set_thread_model(db, parent, "GPT-4", concrete_model_info=concrete, reason="user")

    # Verify parent has the model
    assert current_thread_model(db, parent) == "GPT-4"
    assert current_thread_model_info(db, parent) == concrete

    # Create child WITHOUT specifying initial_model_key - should inherit
    child = create_child_thread(db, parent, name="Child")

    # Verify child inherited the model
    assert current_thread_model(db, child) == "GPT-4"
    child_concrete = current_thread_model_info(db, child)
    assert child_concrete == concrete

    # Verify the inheritance event
    events = db.conn.execute(
        "SELECT payload_json FROM events WHERE thread_id=? AND type='model.switch'",
        (child,)
    ).fetchall()
    assert len(events) == 1
    payload = json.loads(events[0][0])
    assert payload['model_key'] == 'GPT-4'
    assert payload['reason'] == 'inherited'
    assert payload['concrete_model_info'] == concrete


def test_child_thread_override_model(tmp_path, monkeypatch):
    """Test that child can override parent's model with explicit initial_model_key."""
    monkeypatch.chdir(tmp_path)

    from eggthreads import (
        ThreadsDB, create_root_thread, create_child_thread,
        set_thread_model, current_thread_model, current_thread_model_info
    )

    db = ThreadsDB()
    db.init_schema()

    # Create models.json for the override model
    models_json = tmp_path / "models.json"
    models_json.write_text(json.dumps({
        "default_model": "GPT-3.5",
        "providers": {
            "openai": {
                "api_base": "https://api.openai.com/v1/chat/completions",
                "api_key_env": "OPENAI_API_KEY",
                "models": {
                    "GPT-3.5": {
                        "model_name": "gpt-3.5-turbo",
                        "max_tokens": 4096,
                    }
                }
            }
        }
    }))

    # Create parent thread with GPT-4
    parent = create_root_thread(db, name="Parent")
    parent_concrete = {
        "providers": {
            "openai": {
                "api_base": "https://api.openai.com/v1/chat/completions",
                "api_key_env": "OPENAI_API_KEY",
                "models": {
                    "GPT-4": {
                        "model_name": "gpt-4",
                        "max_tokens": 8192,
                    }
                }
            }
        }
    }
    set_thread_model(db, parent, "GPT-4", concrete_model_info=parent_concrete, reason="user")

    # Create child WITH explicit initial_model_key - should override
    child = create_child_thread(db, parent, name="Child", initial_model_key="GPT-3.5",
                                 models_path=str(models_json))

    # Verify child has the overridden model (not inherited)
    assert current_thread_model(db, child) == "GPT-3.5"
    # Should have its own concrete_model_info from models.json
    child_concrete = current_thread_model_info(db, child)
    assert child_concrete is not None
    assert "GPT-3.5" in child_concrete['providers']['openai']['models']


def test_spawn_agent_inherits_model(tmp_path, monkeypatch):
    """Test that spawn_agent tool inherits parent's model."""
    monkeypatch.chdir(tmp_path)

    from eggthreads import (
        ThreadsDB, create_root_thread, set_thread_model,
        current_thread_model, current_thread_model_info, append_message
    )
    from eggthreads.tools import create_default_tools

    db = ThreadsDB()
    db.init_schema()

    # Create parent thread with model
    parent = create_root_thread(db, name="Parent")
    append_message(db, parent, "system", "You are a helpful assistant.")

    concrete = {
        "providers": {
            "anthropic": {
                "api_base": "https://api.anthropic.com/v1/messages",
                "api_key_env": "ANTHROPIC_API_KEY",
                "models": {
                    "Claude-3": {
                        "model_name": "claude-3-opus",
                        "max_tokens": 200000,
                    }
                }
            }
        }
    }
    set_thread_model(db, parent, "Claude-3", concrete_model_info=concrete, reason="user")

    # Use spawn_agent tool to create child
    tools = create_default_tools()
    result = tools.execute('spawn_agent', {
        'parent_thread_id': parent,
        'context_text': 'Do something',
        'label': 'spawned',
    })

    # Result should be the child thread ID
    assert isinstance(result, str)
    assert len(result) > 0
    child = result

    # Verify child inherited the model
    assert current_thread_model(db, child) == "Claude-3"
    child_concrete = current_thread_model_info(db, child)
    assert child_concrete == concrete


def test_spawn_agent_ignores_explicit_model_selection_for_model_calls(tmp_path, monkeypatch):
    """Model-initiated spawn_agent calls should ignore explicit model selection."""
    monkeypatch.chdir(tmp_path)

    from eggthreads import (
        ThreadsDB, create_root_thread, set_thread_model,
        current_thread_model, current_thread_model_info, append_message
    )
    from eggthreads.tools import create_default_tools

    db = ThreadsDB()
    db.init_schema()

    models_json = tmp_path / "models.json"
    models_json.write_text(json.dumps({
        "default_model": "GPT-3.5",
        "providers": {
            "openai": {
                "api_base": "https://api.openai.com/v1/chat/completions",
                "api_key_env": "OPENAI_API_KEY",
                "models": {
                    "GPT-3.5": {"model_name": "gpt-3.5-turbo"},
                    "GPT-4": {"model_name": "gpt-4"},
                }
            }
        }
    }))

    parent = create_root_thread(db, name="Parent", models_path=str(models_json))
    append_message(db, parent, "system", "You are a helpful assistant.")

    concrete = {
        "providers": {
            "openai": {
                "api_base": "https://api.openai.com/v1/chat/completions",
                "api_key_env": "OPENAI_API_KEY",
                "models": {
                    "GPT-4": {
                        "model_name": "gpt-4",
                        "max_tokens": 8192,
                    }
                }
            }
        }
    }
    set_thread_model(db, parent, "GPT-4", concrete_model_info=concrete, reason="user")

    tools = create_default_tools()
    child = tools.execute('spawn_agent', {
        'context_text': 'Do something',
        'label': 'spawned',
        'initial_model_key': 'GPT-3.5',
    }, thread_id=parent, initial_model_key='GPT-4')

    assert current_thread_model(db, child) == "GPT-4"
    assert current_thread_model_info(db, child) == concrete


def test_spawn_agent_allows_explicit_model_for_direct_callers(tmp_path, monkeypatch):
    """Direct/local callers can still override the model explicitly."""
    monkeypatch.chdir(tmp_path)

    from eggthreads import (
        ThreadsDB, create_root_thread, set_thread_model,
        current_thread_model, append_message
    )
    from eggthreads.tools import create_default_tools

    db = ThreadsDB()
    db.init_schema()

    models_json = tmp_path / "models.json"
    models_json.write_text(json.dumps({
        "default_model": "GPT-3.5",
        "providers": {
            "openai": {
                "api_base": "https://api.openai.com/v1/chat/completions",
                "api_key_env": "OPENAI_API_KEY",
                "models": {
                    "GPT-3.5": {"model_name": "gpt-3.5-turbo"},
                    "GPT-4": {"model_name": "gpt-4"},
                }
            }
        }
    }))

    parent = create_root_thread(db, name="Parent", models_path=str(models_json))
    append_message(db, parent, "system", "You are a helpful assistant.")
    set_thread_model(db, parent, "GPT-4", reason="user", models_path=str(models_json))

    tools = create_default_tools()
    child = tools.execute('spawn_agent', {
        'parent_thread_id': parent,
        'context_text': 'Do something',
        'label': 'spawned',
        'initial_model_key': 'GPT-3.5',
    })

    assert current_thread_model(db, child) == "GPT-3.5"


def test_spawn_agent_inherits_latest_parent_model_after_parent_switch(tmp_path, monkeypatch):
    """New children should inherit the parent's latest effective model."""
    monkeypatch.chdir(tmp_path)

    from eggthreads import (
        ThreadsDB, create_root_thread, set_thread_model,
        current_thread_model, current_thread_model_info, append_message,
    )
    from eggthreads.tools import create_default_tools

    db = ThreadsDB()
    db.init_schema()

    parent = create_root_thread(db, name="Parent")
    append_message(db, parent, "system", "You are a helpful assistant.")

    first = {
        "providers": {
            "openai": {
                "api_base": "https://api.openai.com/v1/chat/completions",
                "api_key_env": "OPENAI_API_KEY",
                "models": {
                    "GPT-4": {"model_name": "gpt-4"}
                }
            }
        }
    }
    second = {
        "providers": {
            "anthropic": {
                "api_base": "https://api.anthropic.com/v1/messages",
                "api_key_env": "ANTHROPIC_API_KEY",
                "models": {
                    "Claude-3": {"model_name": "claude-3-opus"}
                }
            }
        }
    }
    set_thread_model(db, parent, "GPT-4", concrete_model_info=first, reason="user")
    set_thread_model(db, parent, "Claude-3", concrete_model_info=second, reason="user")

    tools = create_default_tools()
    child = tools.execute('spawn_agent', {
        'context_text': 'Do something',
        'label': 'spawned',
    }, thread_id=parent, initial_model_key='Claude-3')

    assert current_thread_model(db, child) == "Claude-3"
    assert current_thread_model_info(db, child) == second


def test_spawned_child_can_change_model_after_spawn(tmp_path, monkeypatch):
    """A spawned child should still honor later model changes on itself."""
    monkeypatch.chdir(tmp_path)

    from eggthreads import (
        ThreadsDB, create_root_thread, set_thread_model,
        current_thread_model, current_thread_model_info, append_message,
    )
    from eggthreads.tools import create_default_tools

    db = ThreadsDB()
    db.init_schema()

    parent = create_root_thread(db, name="Parent")
    append_message(db, parent, "system", "You are a helpful assistant.")

    inherited = {
        "providers": {
            "openai": {
                "api_base": "https://api.openai.com/v1/chat/completions",
                "api_key_env": "OPENAI_API_KEY",
                "models": {
                    "GPT-4": {"model_name": "gpt-4"}
                }
            }
        }
    }
    updated = {
        "providers": {
            "openai": {
                "api_base": "https://api.openai.com/v1/chat/completions",
                "api_key_env": "OPENAI_API_KEY",
                "models": {
                    "GPT-3.5": {"model_name": "gpt-3.5-turbo"}
                }
            }
        }
    }
    set_thread_model(db, parent, "GPT-4", concrete_model_info=inherited, reason="user")

    tools = create_default_tools()
    child = tools.execute('spawn_agent', {
        'context_text': 'Do something',
        'label': 'spawned',
    }, thread_id=parent, initial_model_key='GPT-4')

    assert current_thread_model(db, child) == "GPT-4"
    assert current_thread_model_info(db, child) == inherited

    set_thread_model(db, child, "GPT-3.5", concrete_model_info=updated, reason="user")

    assert current_thread_model(db, child) == "GPT-3.5"
    assert current_thread_model_info(db, child) == updated


def test_spawn_tools_schema_hides_model_selection(tmp_path, monkeypatch):
    """spawn tool specs should not expose an initial_model_key parameter."""
    monkeypatch.chdir(tmp_path)

    from eggthreads.tools import create_default_tools

    tools = create_default_tools()
    specs = tools.tools_spec()
    by_name = {spec['function']['name']: spec for spec in specs}

    for name in ('spawn_agent', 'spawn_agent_auto'):
        props = by_name[name]['function']['parameters']['properties']
        assert 'initial_model_key' not in props


def test_spawn_agent_auto_inherits_model(tmp_path, monkeypatch):
    """spawn_agent_auto should inherit the parent's model."""
    monkeypatch.chdir(tmp_path)

    from eggthreads import (
        ThreadsDB, create_root_thread, set_thread_model,
        current_thread_model, current_thread_model_info, append_message,
        build_tool_call_states,
    )
    from eggthreads.tools import create_default_tools

    db = ThreadsDB()
    db.init_schema()

    parent = create_root_thread(db, name="Parent")
    append_message(db, parent, "system", "You are a helpful assistant.")

    concrete = {
        "providers": {
            "anthropic": {
                "api_base": "https://api.anthropic.com/v1/messages",
                "api_key_env": "ANTHROPIC_API_KEY",
                "models": {
                    "Claude-3": {
                        "model_name": "claude-3-opus",
                        "max_tokens": 200000,
                    }
                }
            }
        }
    }
    set_thread_model(db, parent, "Claude-3", concrete_model_info=concrete, reason="user")

    tools = create_default_tools()
    child = tools.execute('spawn_agent_auto', {
        'context_text': 'Do something',
        'label': 'spawned-auto',
        'initial_model_key': 'SomeOtherModel',
    }, thread_id=parent, initial_model_key='Claude-3')

    assert current_thread_model(db, child) == "Claude-3"
    assert current_thread_model_info(db, child) == concrete

    approvals = db.conn.execute(
        "SELECT payload_json FROM events WHERE thread_id=? AND type='tool_call.approval'",
        (child,),
    ).fetchall()
    assert approvals
    payload = json.loads(approvals[-1][0])
    assert payload['decision'] == 'global_approval'


def test_grandchild_inherits_model(tmp_path, monkeypatch):
    """Test that grandchild inherits model through the chain."""
    monkeypatch.chdir(tmp_path)

    from eggthreads import (
        ThreadsDB, create_root_thread, create_child_thread,
        set_thread_model, current_thread_model, current_thread_model_info
    )

    db = ThreadsDB()
    db.init_schema()

    # Create root with model
    root = create_root_thread(db, name="Root")
    concrete = {
        "providers": {
            "openai": {
                "api_base": "https://api.openai.com/v1/chat/completions",
                "api_key_env": "OPENAI_API_KEY",
                "models": {
                    "GPT-4": {
                        "model_name": "gpt-4",
                        "max_tokens": 8192,
                    }
                }
            }
        }
    }
    set_thread_model(db, root, "GPT-4", concrete_model_info=concrete, reason="user")

    # Create child (inherits from root)
    child = create_child_thread(db, root, name="Child")
    assert current_thread_model(db, child) == "GPT-4"

    # Create grandchild (inherits from child, which inherited from root)
    grandchild = create_child_thread(db, child, name="Grandchild")

    # Verify grandchild has the same model
    assert current_thread_model(db, grandchild) == "GPT-4"
    assert current_thread_model_info(db, grandchild) == concrete


def test_no_model_no_inheritance(tmp_path, monkeypatch):
    """Test that child of parent without model also has no model."""
    monkeypatch.chdir(tmp_path)

    from eggthreads import (
        ThreadsDB, create_root_thread, create_child_thread,
        current_thread_model, current_thread_model_info
    )

    db = ThreadsDB()
    db.init_schema()

    # Create root WITHOUT setting a model
    root = create_root_thread(db, name="Root")
    assert current_thread_model(db, root) is None

    # Create child - should also have no model (nothing to inherit)
    child = create_child_thread(db, root, name="Child")
    assert current_thread_model(db, child) is None
    assert current_thread_model_info(db, child) is None


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
