"""Shared EggW image-generation execution helpers."""
from __future__ import annotations

import asyncio
from typing import Any, Mapping

from eggthreads import append_message, create_snapshot
from eggthreads.artifact_completion import artifact_workspace_from_db
from eggthreads.image_generation import (
    ImageGenerationArtifactResult,
    generate_openai_image_artifacts,
    image_generation_result_content_parts,
)

from . import core


async def generate_and_append_thread_image(
    thread_id: str,
    prompt: str,
    *,
    model_key: str | None,
    options: Mapping[str, Any] | None,
) -> tuple[ImageGenerationArtifactResult, list[dict[str, Any]], str]:
    """Generate provider-backed images and append canonical artifact parts.

    Used by both the structured EggW API route and the textual
    ``/imageGenerate`` command so storage, transcript append, and snapshot
    behavior cannot drift.
    """

    if not core.db:
        raise RuntimeError("Database not initialized")

    result = await asyncio.to_thread(
        generate_openai_image_artifacts,
        artifact_workspace_from_db(core.db),
        thread_id,
        prompt,
        model_key=model_key,
        models_path=core.MODELS_PATH,
        all_models_path=core.ALL_MODELS_PATH,
        image_generation_models_path=core.IMAGE_GENERATION_MODELS_PATH,
        options=options,
    )
    content_parts = image_generation_result_content_parts(result)
    message_id = append_message(core.db, thread_id, role="assistant", content=content_parts)
    create_snapshot(core.db, thread_id)
    return result, content_parts, message_id


__all__ = ["generate_and_append_thread_image", "generate_openai_image_artifacts"]
