"""Image-generation slash commands for eggw backend."""
from __future__ import annotations

import os
from pathlib import Path

from eggllm.image_generation import ImageGenerationConfigError, ImageGenerationError, ImageGenerationProviderError
from eggthreads.artifact_completion import artifact_workspace_from_db
from eggthreads.image_generation import (
    format_image_generation_start_message,
    format_image_generation_artifact_result,
    parse_image_generate_args,
)

from .. import core
from ..image_generation_service import generate_and_append_thread_image
from ..models import CommandResponse


def _display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(artifact_workspace_from_db(core.db)))
    except Exception:
        return str(path)


async def cmd_image_generate(thread_id: str, arg: str) -> CommandResponse:
    """Generate provider-backed image artifacts from the textual command."""

    if not core.db:
        return CommandResponse(success=False, message="/imageGenerate failed: database not initialized")
    if not core.db.get_thread(thread_id):
        return CommandResponse(success=False, message="/imageGenerate failed: thread not found")

    try:
        prompt, model_key, options = parse_image_generate_args(arg)
    except ValueError as e:
        return CommandResponse(success=False, message=str(e))

    try:
        core.db.append_event(
            event_id=os.urandom(10).hex(),
            thread_id=thread_id,
            type_="user_command.status",
            payload={
                "command_name": "imageGenerate",
                "message": format_image_generation_start_message(model_key=model_key, prompt=prompt),
                "timeout": 600,
            },
        )
    except Exception:
        pass

    try:
        result, content_parts, message_id = await generate_and_append_thread_image(
            thread_id,
            prompt,
            model_key=model_key,
            options=options,
        )
    except (ImageGenerationConfigError, ImageGenerationProviderError, ImageGenerationError) as e:
        return CommandResponse(success=False, message=f"/imageGenerate failed: {e}")
    except Exception as e:
        return CommandResponse(success=False, message=f"/imageGenerate failed: {e}")

    message = format_image_generation_artifact_result(result, content_parts, display_path=_display_path)
    return CommandResponse(
        success=True,
        message=message,
        data={
            "action": "image_generation",
            "reload": True,
            "message_id": message_id,
            "artifact_ids": [artifact.artifact_id for artifact in result.artifacts],
            "image_model_key": result.model_key,
            "image_provider_name": result.provider_name,
            "image_model_name": result.model_name,
        },
    )


__all__ = ["cmd_image_generate"]
