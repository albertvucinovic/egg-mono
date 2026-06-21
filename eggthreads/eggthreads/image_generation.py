from __future__ import annotations

"""Thread-owned storage service for provider-backed image generation."""

import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from eggllm.config import load_image_generation_models_config
from eggllm.image_generation import ImageGenerationResult, generate_images

from .content_parts import artifact_part_from_provider_output_metadata, content_to_plain_text
from .provider_output_artifacts import SavedProviderOutputArtifact, save_provider_output_bytes


IMAGE_GENERATE_OPTION_KEYS = {
    "background",
    "model",
    "backend",
    "n",
    "output_format",
    "quality",
    "size",
}


def image_generate_usage() -> str:
    return (
        "Usage: /imageGenerate [model=<backend>] [n=<1-10>] [size=<size>] "
        "[quality=<quality>] [output_format=<png|jpeg|webp>] "
        "[background=<background>] <prompt>"
    )


def format_image_generation_start_message(*, model_key: str | None, prompt: str) -> str:
    """Return short user-facing text for an image-generation command start."""

    label = model_key or "default image model"
    clean_prompt = " ".join(str(prompt or "").split())
    short_prompt = clean_prompt if len(clean_prompt) <= 120 else clean_prompt[:117].rstrip() + "..."
    return f"Generating image with {label}: {short_prompt}"


def parse_image_generate_args(arg: str) -> tuple[str, str | None, dict[str, Any]]:
    """Parse the textual ``/imageGenerate`` command arguments.

    Named options are parsed only before the first prompt token (or before
    ``--``).  This keeps arbitrary prompts predictable while still supporting a
    compact command form in both terminal Egg and EggW.
    """

    text = str(arg or "").strip()
    if not text:
        raise ValueError(image_generate_usage())
    try:
        tokens = shlex.split(text)
    except ValueError as e:
        raise ValueError(f"Could not parse /imageGenerate arguments: {e}") from e
    if not tokens:
        raise ValueError(image_generate_usage())

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
            if key in IMAGE_GENERATE_OPTION_KEYS:
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
        raise ValueError(image_generate_usage())
    return prompt, model_key, options


def configured_image_generation_backend_keys(
    *,
    image_generation_models_path: str | Path | None = None,
    models_path: str | Path = "models.json",
) -> list[str]:
    try:
        models_config, _providers_config = load_image_generation_models_config(
            image_generation_models_path,
            models_path=models_path,
        )
        return list(models_config.keys())
    except Exception:
        return []


def complete_image_generate_args(
    arg: str,
    *,
    image_generation_models_path: str | Path | None = None,
    models_path: str | Path = "models.json",
) -> list[str]:
    """Return UI-neutral completions for ``/imageGenerate`` arguments."""

    text = str(arg or "")
    current = text.rsplit(None, 1)[-1] if text and not text.endswith(" ") else ""

    for prefix in ("model=", "backend="):
        if current.startswith(prefix):
            partial = current[len(prefix):].strip("'\"").lower()
            return [
                f"{prefix}{shlex.quote(key)}"
                for key in configured_image_generation_backend_keys(
                    image_generation_models_path=image_generation_models_path,
                    models_path=models_path,
                )
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


def normalize_image_generation_model_key(model: Any = None, backend: Any = None) -> str | None:
    """Return the explicit image-generation backend key from model/backend aliases.

    Egg's user/API/tool surfaces accept both names for readability.  When both
    are supplied they must identify the same configured backend so callers do
    not accidentally send prompts to an unexpected image model.
    """

    model_key = str(model).strip() if isinstance(model, str) and str(model).strip() else None
    backend_key = str(backend).strip() if isinstance(backend, str) and str(backend).strip() else None
    if model_key and backend_key and model_key != backend_key:
        raise ValueError("model and backend must match when both are provided")
    return model_key or backend_key


def normalize_openai_image_generation_options(values: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return the small safe OpenAI Images option set accepted by Egg surfaces."""

    if not isinstance(values, Mapping):
        return {}
    options: dict[str, Any] = {}

    n = values.get("n")
    if n is not None:
        if isinstance(n, bool):
            raise ValueError("n must be an integer from 1 to 10")
        try:
            n_value = int(n)
        except (TypeError, ValueError) as e:
            raise ValueError("n must be an integer from 1 to 10") from e
        if str(n).strip() != str(n_value) and not isinstance(n, int):
            raise ValueError("n must be an integer from 1 to 10")
        if n_value < 1 or n_value > 10:
            raise ValueError("n must be an integer from 1 to 10")
        options["n"] = n_value

    for key in ("size", "quality", "background"):
        value = values.get(key)
        if isinstance(value, str) and value.strip():
            options[key] = value.strip()

    output_format = values.get("output_format")
    if isinstance(output_format, str) and output_format.strip():
        fmt = output_format.strip().lower().lstrip(".")
        if fmt == "jpg":
            fmt = "jpeg"
        if fmt not in {"png", "jpeg", "webp"}:
            raise ValueError("output_format must be png, jpeg, or webp")
        options["output_format"] = fmt

    return options


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


def image_generation_result_content_parts(
    result: ImageGenerationArtifactResult,
) -> list[dict[str, Any]]:
    """Return textual summary plus canonical artifact parts for a result.

    User commands and APIs should store/display generated images as provider
    artifact references, never as inline bytes/base64.  Keeping this small
    presentation shape here lets terminal and web entrypoints share the same
    default transcript representation.
    """

    count = len(result.artifacts)
    noun = "image artifact" if count == 1 else "image artifacts"
    model_label = result.model_key or result.model_name
    summary = f"Generated {count} {noun} via {model_label} ({result.model_name}).\nPrompt: {result.prompt}"
    return [{"type": "text", "text": summary}, *result.content_parts]


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


def format_image_generation_artifact_result(
    result: ImageGenerationArtifactResult,
    content: list[dict[str, Any]] | None = None,
    *,
    display_path: Callable[[Path], str] | None = None,
) -> str:
    """Return user-facing result text with export/reuse hints.

    Command surfaces should append canonical provider-output artifact parts to
    the transcript, then show a text summary that helps users discover the
    generated artifact ids and the follow-up commands that act on them.
    """

    parts = content if content is not None else image_generation_result_content_parts(result)
    lines = [content_to_plain_text(parts, validate=True).strip()]
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
        if record_dir is not None:
            stored = display_path(Path(record_dir)) if display_path is not None else str(Path(record_dir))
        else:
            stored = ".egg/egg_provider_output"
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
    api_type = str(image_metadata.get("api_type") or "openai_images")
    kind = "codex_image_generation" if api_type == "codex_images" else "openai_image_generation"
    provenance: dict[str, Any] = {
        "kind": kind,
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
    api_type = str(image_metadata.get("api_type") or "openai_images")
    openai_refs: dict[str, Any] = {
        "api_type": api_type,
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
    image_generation_models_path: str | Path | None = None,
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

    generate = backend_generate_func or generate_images
    result = generate(
        prompt,
        model_key=model_key,
        models_path=models_path,
        all_models_path=all_models_path,
        image_generation_models_path=image_generation_models_path,
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
    "IMAGE_GENERATE_OPTION_KEYS",
    "GeneratedProviderOutputArtifact",
    "ImageGenerationArtifactResult",
    "complete_image_generate_args",
    "configured_image_generation_backend_keys",
    "format_image_generation_start_message",
    "format_image_generation_artifact_result",
    "generate_openai_image_artifacts",
    "image_generation_result_content_parts",
    "image_generate_usage",
    "normalize_image_generation_model_key",
    "normalize_openai_image_generation_options",
    "parse_image_generate_args",
]
