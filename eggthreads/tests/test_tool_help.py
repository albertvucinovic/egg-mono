from __future__ import annotations

import json

from eggthreads.tool_help import BUILTIN_TOOL_HELP_DETAILS, missing_detailed_tool_help_entries
from eggthreads.tools import create_default_tools


def _specs_by_name(registry):
    return {spec["function"]["name"]: spec for spec in registry.tools_spec()}


def test_tool_help_is_registered_and_lists_default_tools() -> None:
    registry = create_default_tools()
    specs = _specs_by_name(registry)

    assert "tool_help" in specs
    spec = specs["tool_help"]
    assert "Describe Egg tools" in spec["function"]["description"]
    props = spec["function"]["parameters"]["properties"]
    assert {"tool_name", "include_schema", "include_unavailable"}.issubset(props)

    output = registry.execute("tool_help", {})
    assert "Egg tool help" in output
    assert "`generate_image`" in output
    assert "`tool_help`" in output


def test_every_default_registered_tool_has_curated_detailed_help() -> None:
    registry = create_default_tools()
    registered_tool_names = list(registry._tools.keys())

    missing = missing_detailed_tool_help_entries(registered_tool_names)

    assert missing == []
    # Guard the intent of the test: this should cover more than just the new tool.
    assert "generate_image" in BUILTIN_TOOL_HELP_DETAILS
    assert "bash" in BUILTIN_TOOL_HELP_DETAILS


def test_tool_help_for_generate_image_includes_dynamic_config(tmp_path) -> None:
    models_path = tmp_path / "models.json"
    image_models_path = tmp_path / "image-generation-models.json"
    models_path.write_text(
        json.dumps(
            {
                "providers": {
                    "openai": {
                        "api_base": "https://api.openai.com/v1",
                        "api_key_env": "OPENAI_API_KEY",
                        "models": {"OpenAI Chat": {"model_name": "gpt-4o-mini"}},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    image_models_path.write_text(
        json.dumps(
            {
                "default": "OpenAI Image: gpt-image-1",
                "models": {
                    "OpenAI Image: gpt-image-1": {
                        "provider": "openai",
                        "api_type": "openai_images",
                        "model_name": "gpt-image-1",
                        "alias": ["gpt-image"],
                    },
                    "OpenAI Pro Image: gpt-image-2": {
                        "provider": "openai",
                        "api_type": "codex_images",
                        "model_name": "gpt-image-2",
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    registry = create_default_tools()
    output = registry.execute(
        "tool_help",
        {"tool_name": "generate_image"},
        models_path=str(models_path),
        image_generation_models_path=str(image_models_path),
    )

    assert "Dynamic image-generation context:" in output
    assert "Default model when `model`/`backend` is omitted: `OpenAI Image: gpt-image-1`" in output
    assert "`OpenAI Image: gpt-image-1` (default)" in output
    assert "aliases: `gpt-image`" in output
    assert "api_type: `openai_images`" in output
    assert "`OpenAI Pro Image: gpt-image-2`" in output
    assert "api_type: `codex_images`" in output
    assert "endpoint note: posts to `images/generations`" in output
    assert "Recommended call: omit `model`" in output


def test_tool_help_can_include_raw_schema_for_selected_tool() -> None:
    registry = create_default_tools()

    output = registry.execute("tool_help", {"tool_name": "bash", "include_schema": True})

    assert "Tool: bash" in output
    assert "Parameters:" in output
    assert "`script` (string, required)" in output
    assert "Spec (sent to LLM):" in output
    assert '"name": "bash"' in output


def test_tool_help_reports_unknown_tool_with_available_names() -> None:
    registry = create_default_tools()

    output = registry.execute("tool_help", {"tool_name": "does_not_exist"})

    assert "Tool 'does_not_exist' not found." in output
    assert "generate_image" in output
    assert "tool_help" in output


def test_tool_help_reflects_thread_tool_allowlist(tmp_path) -> None:
    import eggthreads as ts

    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="root")
    ts.set_thread_tool_allowlist(db, tid, ["tool_help"])

    registry = create_default_tools()

    listing = registry.execute("tool_help", {}, db=db, thread_id=tid)
    assert "`tool_help`" in listing
    assert "`bash`" not in listing

    bash_help = registry.execute("tool_help", {"tool_name": "bash"}, db=db, thread_id=tid)
    assert "Tool: bash" in bash_help
    assert "Status: not allowed for this thread" in bash_help


def test_tool_info_command_uses_shared_dynamic_generate_image_help(tmp_path) -> None:
    from eggthreads.command_catalog import CommandContext, create_default_command_registry

    models_path = tmp_path / "models.json"
    image_models_path = tmp_path / "image-generation-models.json"
    models_path.write_text(
        json.dumps(
            {
                "providers": {
                    "openai": {
                        "api_base": "https://api.openai.com/v1",
                        "api_key_env": "OPENAI_API_KEY",
                        "models": {},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    image_models_path.write_text(
        json.dumps(
            {
                "default_model": "OpenAI Image: gpt-image-1",
                "models": {
                    "OpenAI Image: gpt-image-1": {
                        "provider": "openai",
                        "api_type": "openai_images",
                        "model_name": "gpt-image-1",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    printed: list[tuple[str, str]] = []

    result = create_default_command_registry().execute(
        "toolInfo",
        CommandContext(
            current_thread="thread-1",
            models_path=models_path,
            image_generation_models_path=image_models_path,
            console_print_block=lambda title, text, **kwargs: printed.append((title, text)),
        ),
        "generate_image",
    )

    assert result.clear_input is True
    assert printed
    text = printed[0][1]
    assert "Dynamic image-generation context:" in text
    assert "Default model when `model`/`backend` is omitted: `OpenAI Image: gpt-image-1`" in text
    assert "`OpenAI Image: gpt-image-1` (default)" in text


def test_generate_image_dynamic_help_marks_alias_default(tmp_path) -> None:
    models_path = tmp_path / "models.json"
    image_models_path = tmp_path / "image-generation-models.json"
    models_path.write_text(
        json.dumps(
            {
                "providers": {
                    "openai": {
                        "api_base": "https://api.openai.com/v1",
                        "api_key_env": "OPENAI_API_KEY",
                        "models": {},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    image_models_path.write_text(
        json.dumps(
            {
                "default": "image-alias",
                "models": {
                    "OpenAI Image: gpt-image-1": {
                        "provider": "openai",
                        "api_type": "openai_images",
                        "model_name": "gpt-image-1",
                        "alias": ["image-alias"],
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    output = create_default_tools().execute(
        "tool_help",
        {"tool_name": "generate_image"},
        models_path=str(models_path),
        image_generation_models_path=str(image_models_path),
    )

    assert "`OpenAI Image: gpt-image-1` (configured as `image-alias`)" in output
    assert "`OpenAI Image: gpt-image-1` (default)" in output
