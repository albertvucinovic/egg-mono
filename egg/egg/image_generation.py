from __future__ import annotations

"""Terminal image-generation slash command for Egg."""

import shlex
import asyncio
from pathlib import Path
from typing import Any

from eggllm.config import load_image_generation_models_config
from eggthreads.command_catalog import CommandResult, CommandSpec
from eggthreads.content_parts import content_to_plain_text
from eggthreads.image_generation import (
    ImageGenerationArtifactResult,
    generate_openai_image_artifacts,
    image_generation_result_content_parts,
)

from .utils import ALL_MODELS_PATH, IMAGE_GENERATION_MODELS_PATH, MODELS_PATH

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


def _format_bytes(size_bytes: Any) -> str:
    try:
        size = int(size_bytes)
    except Exception:
        return "unknown size"
    if size < 0:
        return "unknown size"
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(size)
    unit_index = 0
    while value >= 1024 and unit_index < len(units) - 1:
        value /= 1024.0
        unit_index += 1
    if unit_index == 0:
        return f"{int(value)} B"
    if value >= 100:
        rendered = f"{value:.0f}"
    elif value >= 10:
        rendered = f"{value:.1f}".rstrip("0").rstrip(".")
    else:
        rendered = f"{value:.2f}".rstrip("0").rstrip(".")
    return f"{rendered} {units[unit_index]}"


def _display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except Exception:
        return str(path)


def _format_image_generation_terminal_result(
    result: ImageGenerationArtifactResult,
    content: list[dict[str, Any]],
) -> str:
    """Return terminal-friendly result text with export/reuse hints."""

    lines = [content_to_plain_text(content, validate=True).strip()]
    if result.artifacts:
        lines.extend(["", "Artifacts:"])
    for artifact in result.artifacts:
        metadata = getattr(artifact, "metadata", None)
        if not isinstance(metadata, dict):
            metadata = getattr(artifact, "content_part", None)
        metadata = dict(metadata) if isinstance(metadata, dict) else {}
        artifact_id = str(getattr(artifact, "artifact_id", "") or metadata.get("artifact_id") or "unknown")
        filename = str(metadata.get("filename") or f"{artifact_id}.bin")
        mime_type = str(metadata.get("mime_type") or "application/octet-stream")
        presentation = str(metadata.get("presentation") or "file")
        size = _format_bytes(metadata.get("size_bytes"))
        saved = getattr(artifact, "saved", None)
        record_dir = getattr(saved, "record_dir", None)
        stored = _display_path(Path(record_dir)) if record_dir is not None else ".egg/egg_provider_output"
        lines.extend(
            [
                f"- id: {artifact_id}",
                f"  file: {filename}",
                f"  type: {mime_type} ({presentation}, {size})",
                f"  stored: {stored}",
                f"  export: /saveProviderArtifact {artifact_id} {shlex.quote(filename)}",
                f"  reuse: /attachOutput {artifact_id}",
            ]
        )
    return "\n".join(line for line in lines if line is not None).strip()


def _finish_image_generation_command(ctx: Any, result: ImageGenerationArtifactResult) -> CommandResult:
    content = _append_result_message(ctx, result)
    message = _format_image_generation_terminal_result(result, content)
    _log_or_print_result(ctx, message)
    return CommandResult(clear_input=True, message=message)


def _log_generation_start(ctx: Any, *, model_key: str | None, prompt: str) -> None:
    logger = getattr(ctx, "log_system", None)
    if callable(logger):
        label = model_key or "default image model"
        short_prompt = prompt if len(prompt) <= 120 else prompt[:117].rstrip() + "..."
        logger(f"Generating image with {label}: {short_prompt}")


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
        prompt, model_key, options = _parse_image_generate_args(arg)
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
    text = str(arg or "")
    current = text.rsplit(None, 1)[-1] if text and not text.endswith(" ") else ""

    def image_backend_keys() -> list[str]:
        try:
            models_config, _providers_config = load_image_generation_models_config(
                IMAGE_GENERATION_MODELS_PATH,
                models_path=MODELS_PATH,
            )
            return list(models_config.keys())
        except Exception:
            return []

    for prefix in ("model=", "backend="):
        if current.startswith(prefix):
            partial = current[len(prefix):].strip("'\"").lower()
            return [
                f"{prefix}{shlex.quote(key)}"
                for key in image_backend_keys()
                if key.lower().startswith(partial)
            ]

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
