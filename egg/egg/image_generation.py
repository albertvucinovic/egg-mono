from __future__ import annotations

"""Terminal image-generation slash command for Egg."""

import shlex
from pathlib import Path
from typing import Any

from eggthreads.command_catalog import CommandResult, CommandSpec
from eggthreads.content_parts import content_to_plain_text
from eggthreads.image_generation import ImageGenerationArtifactResult, generate_openai_image_artifacts

from .utils import ALL_MODELS_PATH, MODELS_PATH

_IMAGE_GENERATE_COMMAND = "imageGenerate"
_IMAGE_GENERATE_OPTION_KEYS = {
    "background",
    "model",
    "backend",
    "n",
    "output_format",
    "quality",
    "size",
}


def _usage() -> str:
    return (
        "Usage: /imageGenerate [model=<backend>] [n=<1-10>] [size=<size>] "
        "[quality=<quality>] [output_format=<png|jpeg|webp>] "
        "[background=<background>] <prompt>"
    )


def _parse_image_generate_args(arg: str) -> tuple[str, str | None, dict[str, Any]]:
    """Parse small, front-loaded ``/imageGenerate`` options and prompt.

    Named options are only parsed before the first prompt token (or before
    ``--``).  This keeps arbitrary prompt text safe and predictable: use
    ``/imageGenerate -- key=value should be painted`` when a prompt must start
    with a key-like token.
    """

    text = str(arg or "").strip()
    if not text:
        raise ValueError(_usage())
    try:
        tokens = shlex.split(text)
    except ValueError as e:
        raise ValueError(f"Could not parse /imageGenerate arguments: {e}") from e
    if not tokens:
        raise ValueError(_usage())

    prompt_tokens: list[str] = []
    options: dict[str, Any] = {}
    model_key: str | None = None
    parsing_options = True

    for token in tokens:
        if parsing_options and token == "--":
            parsing_options = False
            continue
        if parsing_options and "=" in token:
            raw_key, raw_value = token.split("=", 1)
            key = raw_key.strip()
            value = raw_value.strip()
            if key in _IMAGE_GENERATE_OPTION_KEYS:
                if not value:
                    raise ValueError(f"/imageGenerate option {key}= requires a value.")
                if key in {"model", "backend"}:
                    model_key = value
                elif key == "n":
                    try:
                        n_value = int(value)
                    except ValueError as e:
                        raise ValueError("/imageGenerate option n= must be an integer from 1 to 10.") from e
                    if n_value < 1 or n_value > 10:
                        raise ValueError("/imageGenerate option n= must be an integer from 1 to 10.")
                    options[key] = n_value
                elif key == "output_format":
                    fmt = value.strip().lower().lstrip(".")
                    if fmt == "jpg":
                        fmt = "jpeg"
                    if fmt not in {"png", "jpeg", "webp"}:
                        raise ValueError("/imageGenerate option output_format= must be png, jpeg, or webp.")
                    options[key] = fmt
                else:
                    options[key] = value
                continue
            if not prompt_tokens:
                raise ValueError(f"Unsupported /imageGenerate option: {key}")

        parsing_options = False
        prompt_tokens.append(token)

    prompt = " ".join(prompt_tokens).strip()
    if not prompt:
        raise ValueError(_usage())
    return prompt, model_key, options


def _content_parts_for_result(result: ImageGenerationArtifactResult) -> list[dict[str, Any]]:
    count = len(result.artifacts)
    noun = "image artifact" if count == 1 else "image artifacts"
    model_label = result.model_key or result.model_name
    summary = f"Generated {count} {noun} via {model_label} ({result.model_name}).\nPrompt: {result.prompt}"
    parts: list[dict[str, Any]] = [{"type": "text", "text": summary}]
    parts.extend(result.content_parts)
    return parts


def _append_result_message(ctx: Any, result: ImageGenerationArtifactResult) -> list[dict[str, Any]]:
    db = getattr(ctx, "db", None)
    thread_id = str(getattr(ctx, "current_thread", "") or "").strip()
    if db is None or not thread_id:
        raise ValueError("/imageGenerate requires an active thread.")

    content = _content_parts_for_result(result)
    append = getattr(ctx, "append_message", None)
    if callable(append):
        append(db, thread_id, "assistant", content)
    else:
        from eggthreads import append_message

        append_message(db, thread_id, "assistant", content)

    snapshot = getattr(ctx, "create_snapshot", None)
    if callable(snapshot):
        snapshot(db, thread_id)
    else:
        from eggthreads import create_snapshot

        create_snapshot(db, thread_id)
    return content


def _log_or_print_result(ctx: Any, message: str) -> None:
    printer = getattr(ctx, "console_print_block", None)
    if callable(printer):
        try:
            printer("Image Generation", message, border_style="green", markup=False)
            return
        except TypeError:
            printer("Image Generation", message, border_style="green")
            return
        except Exception:
            pass
    logger = getattr(ctx, "log_system", None)
    if callable(logger):
        logger(message)


def image_generate_command(ctx: Any, arg: str) -> CommandResult:
    """Generate images through the configured backend and append artifact refs."""

    db = getattr(ctx, "db", None)
    thread_id = str(getattr(ctx, "current_thread", "") or "").strip()
    if db is None or not thread_id:
        return CommandResult(clear_input=False, message="/imageGenerate failed: no current thread.")

    try:
        prompt, model_key, options = _parse_image_generate_args(arg)
    except ValueError as e:
        return CommandResult(clear_input=False, message=str(e))

    try:
        result = generate_openai_image_artifacts(
            Path.cwd(),
            thread_id,
            prompt,
            model_key=model_key,
            models_path=MODELS_PATH,
            all_models_path=ALL_MODELS_PATH,
            options=options,
        )
        content = _append_result_message(ctx, result)
        message = content_to_plain_text(content, validate=True)
        _log_or_print_result(ctx, message)
        return CommandResult(clear_input=True, message=message)
    except Exception as e:
        return CommandResult(clear_input=False, message=f"/imageGenerate failed: {e}")


def _complete_image_generate(ctx: Any, arg: str):
    text = str(arg or "")
    fragments = [
        "model=",
        "n=1",
        "size=1024x1024",
        "quality=high",
        "output_format=png",
        "output_format=webp",
        "background=transparent",
        "-- ",
    ]
    current = text.rsplit(None, 1)[-1] if text and not text.endswith(" ") else ""
    return [fragment for fragment in fragments if fragment.startswith(current)]


def register_image_generation_command(registry: Any, app: Any | None = None) -> None:
    """Register terminal-only image generation command if it is not present."""

    try:
        registry.get(_IMAGE_GENERATE_COMMAND)
        return
    except KeyError:
        pass
    registry.register(
        CommandSpec(
            _IMAGE_GENERATE_COMMAND,
            image_generate_command,
            category="image generation",
            usage="/imageGenerate [model=<backend>] [n=<1-10>] [size=<size>] <prompt>",
            description="Generate image artifacts with a configured provider backend.",
            complete=_complete_image_generate,
        )
    )


__all__ = [
    "image_generate_command",
    "register_image_generation_command",
]
