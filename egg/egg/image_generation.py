from __future__ import annotations

"""Terminal image-generation slash command for Egg."""

import asyncio
from pathlib import Path
from typing import Any

from eggthreads.command_catalog import CommandResult, CommandSpec
from eggthreads.image_generation import (
    ImageGenerationArtifactResult,
    complete_image_generate_args,
    format_image_generation_start_message,
    format_image_generation_artifact_result,
    generate_openai_image_artifacts,
    image_generation_result_content_parts,
    parse_image_generate_args,
)

from .utils import ALL_MODELS_PATH, IMAGE_GENERATION_MODELS_PATH, MODELS_PATH

_IMAGE_GENERATE_COMMAND = "imageGenerate"


def _append_result_message(ctx: Any, result: ImageGenerationArtifactResult) -> list[dict[str, Any]]:
    db = getattr(ctx, "db", None)
    thread_id = str(getattr(ctx, "current_thread", "") or "").strip()
    if db is None or not thread_id:
        raise ValueError("/imageGenerate requires an active thread.")

    content = image_generation_result_content_parts(result)
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


def _display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except Exception:
        return str(path)


def _finish_image_generation_command(ctx: Any, result: ImageGenerationArtifactResult) -> CommandResult:
    content = _append_result_message(ctx, result)
    message = format_image_generation_artifact_result(result, content, display_path=_display_path)
    _log_or_print_result(ctx, message)
    return CommandResult(clear_input=True, message=message)


def _log_generation_start(ctx: Any, *, model_key: str | None, prompt: str) -> None:
    logger = getattr(ctx, "log_system", None)
    if callable(logger):
        logger(format_image_generation_start_message(model_key=model_key, prompt=prompt))


def image_generate_command(ctx: Any, arg: str) -> CommandResult:
    """Generate images through the configured backend and append artifact refs."""

    db = getattr(ctx, "db", None)
    thread_id = str(getattr(ctx, "current_thread", "") or "").strip()
    if db is None or not thread_id:
        return CommandResult(clear_input=False, message="/imageGenerate failed: no current thread.")

    try:
        prompt, model_key, options = parse_image_generate_args(arg)
    except ValueError as e:
        return CommandResult(clear_input=False, message=str(e))

    try:
        _log_generation_start(ctx, model_key=model_key, prompt=prompt)
        result = generate_openai_image_artifacts(
            Path.cwd(),
            thread_id,
            prompt,
            model_key=model_key,
            models_path=MODELS_PATH,
            all_models_path=ALL_MODELS_PATH,
            image_generation_models_path=IMAGE_GENERATION_MODELS_PATH,
            options=options,
        )
        return _finish_image_generation_command(ctx, result)
    except Exception as e:
        return CommandResult(clear_input=False, message=f"/imageGenerate failed: {e}")


async def image_generate_command_async(ctx: Any, arg: str) -> CommandResult:
    """Async terminal image generation command for the live UI.

    Provider/network work runs in a worker thread so the terminal can keep
    repainting the ``Streaming[user command; ...]`` indicator while the image
    provider is working.  Transcript append/snapshot still happen on the main
    event-loop thread after the provider call completes.
    """

    db = getattr(ctx, "db", None)
    thread_id = str(getattr(ctx, "current_thread", "") or "").strip()
    if db is None or not thread_id:
        return CommandResult(clear_input=False, message="/imageGenerate failed: no current thread.")

    try:
        prompt, model_key, options = parse_image_generate_args(arg)
    except ValueError as e:
        return CommandResult(clear_input=False, message=str(e))

    try:
        _log_generation_start(ctx, model_key=model_key, prompt=prompt)
        result = await asyncio.to_thread(
            generate_openai_image_artifacts,
            Path.cwd(),
            thread_id,
            prompt,
            model_key=model_key,
            models_path=MODELS_PATH,
            all_models_path=ALL_MODELS_PATH,
            image_generation_models_path=IMAGE_GENERATION_MODELS_PATH,
            options=options,
        )
        return _finish_image_generation_command(ctx, result)
    except Exception as e:
        return CommandResult(clear_input=False, message=f"/imageGenerate failed: {e}")


def _complete_image_generate(ctx: Any, arg: str):
    return complete_image_generate_args(
        arg,
        image_generation_models_path=IMAGE_GENERATION_MODELS_PATH,
        models_path=MODELS_PATH,
    )


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
            image_generate_command_async,
            category="image generation",
            usage="/imageGenerate [model=<backend>] [n=<1-10>] [size=<size>] <prompt>",
            description="Generate image artifacts with a configured provider backend.",
            complete=_complete_image_generate,
        )
    )


__all__ = [
    "image_generate_command_async",
    "image_generate_command",
    "register_image_generation_command",
]
