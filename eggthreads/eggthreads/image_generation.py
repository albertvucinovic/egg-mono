from __future__ import annotations

"""Thread-owned storage service for provider-backed image generation."""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from eggllm.image_generation import ImageGenerationResult, generate_openai_images

from .content_parts import artifact_part_from_provider_output_metadata
from .provider_output_artifacts import SavedProviderOutputArtifact, save_provider_output_bytes


@dataclass(frozen=True)
class GeneratedProviderOutputArtifact:
    """A generated image saved into ``.egg/egg_provider_output``."""

    saved: SavedProviderOutputArtifact
    content_part: dict[str, Any]
    generation_metadata: dict[str, Any]

    @property
    def artifact_id(self) -> str:
        return self.saved.artifact_id

    @property
    def metadata(self) -> dict[str, Any]:
        return self.saved.metadata


@dataclass(frozen=True)
class ImageGenerationArtifactResult:
    """Stored artifacts returned by a provider image-generation request."""

    model_key: str
    provider_name: str
    model_name: str
    prompt: str
    response_metadata: dict[str, Any]
    artifacts: tuple[GeneratedProviderOutputArtifact, ...]

    @property
    def content_parts(self) -> list[dict[str, Any]]:
        return [dict(artifact.content_part) for artifact in self.artifacts]

    @property
    def metadata(self) -> list[dict[str, Any]]:
        return [dict(artifact.metadata) for artifact in self.artifacts]


def _size_dimensions(size: Any) -> dict[str, int]:
    match = re.fullmatch(r"\s*(\d+)\s*x\s*(\d+)\s*", str(size or ""))
    if not match:
        return {}
    return {"width": int(match.group(1)), "height": int(match.group(2))}


def _derived_metadata(image_metadata: Mapping[str, Any], request_options: Mapping[str, Any]) -> dict[str, Any]:
    derived: dict[str, Any] = {}
    derived.update(_size_dimensions(request_options.get("size")))
    for key in ("size", "quality", "output_format", "background"):
        value = request_options.get(key)
        if value is not None:
            derived[key] = value
    revised_prompt = image_metadata.get("revised_prompt")
    if isinstance(revised_prompt, str) and revised_prompt:
        derived["revised_prompt"] = revised_prompt
    return derived


def _provenance(
    *,
    result: ImageGenerationResult,
    image_metadata: Mapping[str, Any],
) -> dict[str, Any]:
    provenance: dict[str, Any] = {
        "kind": "openai_image_generation",
        "provider": result.provider_name,
        "model_key": result.model_key,
        "model": result.model_name,
        "prompt": result.prompt,
        "output_index": image_metadata.get("output_index"),
    }
    revised_prompt = image_metadata.get("revised_prompt")
    if isinstance(revised_prompt, str) and revised_prompt:
        provenance["revised_prompt"] = revised_prompt
    response_id = image_metadata.get("response_id") or result.response_metadata.get("id")
    if response_id is not None:
        provenance["response_id"] = response_id
    return provenance


def _provider_refs(
    *,
    result: ImageGenerationResult,
    image_metadata: Mapping[str, Any],
) -> dict[str, Any]:
    openai_refs: dict[str, Any] = {
        "api_type": "openai_images",
        "model": result.model_name,
        "model_key": result.model_key,
        "output_index": image_metadata.get("output_index"),
        "source": image_metadata.get("source"),
    }
    for key in ("response_id", "response_created", "source_url"):
        value = image_metadata.get(key)
        if value is not None:
            openai_refs[key] = value
    if result.request_options:
        openai_refs["request_options"] = dict(result.request_options)
    return {"openai": openai_refs}


def generate_openai_image_artifacts(
    workspace: Path | str | None,
    thread_id: str,
    prompt: str,
    *,
    model_key: str | None = None,
    models_path: str | Path = "models.json",
    all_models_path: str | Path = "all-models.json",
    options: Mapping[str, Any] | None = None,
    timeout: int = 600,
    session: Any = None,
    backend_generate_func: Callable[..., ImageGenerationResult] | None = None,
) -> ImageGenerationArtifactResult:
    """Generate images and store each output as a provider-output artifact.

    This helper is intentionally storage-only plumbing.  It does not add a user
    command, web route, or LLM-facing tool registration.  Tests can inject a
    fake HTTP ``session`` or a ``backend_generate_func`` to avoid network calls.
    """

    generate = backend_generate_func or generate_openai_images
    result = generate(
        prompt,
        model_key=model_key,
        models_path=models_path,
        all_models_path=all_models_path,
        options=options,
        timeout=timeout,
        session=session,
    )

    artifacts: list[GeneratedProviderOutputArtifact] = []
    for image in result.images:
        image_metadata = dict(image.metadata)
        saved = save_provider_output_bytes(
            workspace,
            thread_id,
            image.data,
            filename=image_metadata.get("filename"),
            mime_type=image_metadata.get("mime_type"),
            presentation="image",
            provenance=_provenance(result=result, image_metadata=image_metadata),
            derived=_derived_metadata(image_metadata, result.request_options),
            provider_refs=_provider_refs(result=result, image_metadata=image_metadata),
        )
        content_part = artifact_part_from_provider_output_metadata(
            saved.metadata,
            provenance=saved.metadata.get("provenance"),
        )
        artifacts.append(
            GeneratedProviderOutputArtifact(
                saved=saved,
                content_part=content_part,
                generation_metadata=image_metadata,
            )
        )

    return ImageGenerationArtifactResult(
        model_key=result.model_key,
        provider_name=result.provider_name,
        model_name=result.model_name,
        prompt=result.prompt,
        response_metadata=dict(result.response_metadata),
        artifacts=tuple(artifacts),
    )


__all__ = [
    "GeneratedProviderOutputArtifact",
    "ImageGenerationArtifactResult",
    "generate_openai_image_artifacts",
]
