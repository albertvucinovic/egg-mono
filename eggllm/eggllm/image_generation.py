from __future__ import annotations

"""Provider-backed image generation helpers.

This module intentionally stays out of the normal chat adapter registry.  Image
backends are discovered from ``models.json`` by explicit task/model-kind
metadata and are called through a small service API.
"""

import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import requests

from .capabilities import is_image_generation_model, supports_task_capability
from .catalog import AllModelsCatalog
from .config import load_models_config
from .provider_http import build_provider_headers
from .registry import ModelRegistry

IMAGE_GENERATION_TASK = "image_generation"
OPENAI_IMAGES_API_TYPE = "openai_images"
OPENAI_IMAGES_GENERATIONS_PATH = "/images/generations"
OPENAI_IMAGES_OPTION_KEYS = frozenset(
    {
        "background",
        "moderation",
        "n",
        "output_compression",
        "output_format",
        "quality",
        "response_format",
        "size",
        "style",
        "user",
    }
)

_OUTPUT_FORMAT_MIME_TYPES = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
}
_MIME_TYPE_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}
_DEFAULT_IMAGE_MIME_TYPE = "image/png"


class ImageGenerationError(RuntimeError):
    """Base class for image generation failures."""


class ImageGenerationConfigError(ValueError):
    """Raised when the configured backend cannot perform image generation."""


class ImageGenerationProviderError(ImageGenerationError):
    """Raised when the provider response cannot be used."""


@dataclass(frozen=True)
class OpenAIImagesBackend:
    """Resolved OpenAI Images backend configuration."""

    model_key: str
    provider_name: str
    provider_config: dict[str, Any]
    model_config: dict[str, Any]
    model_name: str
    url: str


@dataclass(frozen=True)
class GeneratedImage:
    """One provider-generated image and compact non-byte metadata."""

    data: bytes
    metadata: dict[str, Any]


@dataclass(frozen=True)
class ImageGenerationResult:
    """Result of an image-generation provider call.

    ``images`` contains raw bytes for the immediate caller to persist.  The
    metadata dictionaries deliberately exclude image bytes/base64.
    """

    model_key: str
    provider_name: str
    model_name: str
    prompt: str
    request_options: dict[str, Any]
    response_metadata: dict[str, Any]
    images: tuple[GeneratedImage, ...]


def _load_registry(models_path: str | Path, all_models_path: str | Path) -> ModelRegistry:
    models_config, providers_config = load_models_config(models_path)
    return ModelRegistry(models_config, providers_config, AllModelsCatalog(all_models_path))


def _normalized_api_type(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def _filter_openai_images_options(
    options: Mapping[str, Any] | None,
    *,
    reject_unknown: bool,
) -> dict[str, Any]:
    if not isinstance(options, Mapping):
        return {}
    unknown = sorted(
        str(key)
        for key, value in options.items()
        if value is not None and str(key) not in OPENAI_IMAGES_OPTION_KEYS
    )
    if unknown and reject_unknown:
        joined = ", ".join(unknown)
        raise ImageGenerationConfigError(f"Unsupported OpenAI Images option(s): {joined}")
    return {
        str(key): value
        for key, value in options.items()
        if value is not None and str(key) in OPENAI_IMAGES_OPTION_KEYS
    }


def _resolve_openai_images_url(api_base: Any) -> str:
    base = str(api_base or "").strip()
    if not base:
        raise ImageGenerationConfigError("OpenAI Images provider is missing api_base.")
    stripped = base.rstrip("/")
    if stripped.endswith(OPENAI_IMAGES_GENERATIONS_PATH):
        return stripped
    if stripped.endswith("/images"):
        return stripped + "/generations"
    for suffix in ("/chat/completions", "/responses"):
        if stripped.endswith(suffix):
            return stripped[: -len(suffix)] + OPENAI_IMAGES_GENERATIONS_PATH
    if stripped.endswith("/v1"):
        return stripped + OPENAI_IMAGES_GENERATIONS_PATH
    return stripped


def resolve_openai_images_backend(
    model_key: str | None = None,
    *,
    registry: ModelRegistry | None = None,
    models_path: str | Path = "models.json",
    all_models_path: str | Path = "all-models.json",
) -> OpenAIImagesBackend:
    """Resolve and validate an ``api_type: openai_images`` backend.

    If ``model_key`` is omitted, the first configured backend advertising the
    ``image_generation`` task and ``image_generation`` model kind is selected.
    The normal chat model selection is not changed.
    """

    registry = registry or _load_registry(models_path, all_models_path)
    if model_key:
        resolved = registry.resolve(model_key)
        if not resolved:
            raise ImageGenerationConfigError(f"Unknown image generation model: {model_key}")
    else:
        candidates = registry.task_model_keys(IMAGE_GENERATION_TASK, model_kind=IMAGE_GENERATION_TASK)
        resolved = None
        for candidate in candidates:
            candidate_cfg = registry.get_effective_model_config(candidate)
            if _normalized_api_type(candidate_cfg.get("api_type")) == OPENAI_IMAGES_API_TYPE:
                resolved = candidate
                break
        if not resolved:
            raise ImageGenerationConfigError("No api_type: openai_images generation backend is configured.")

    cfg = registry.get_effective_model_config(resolved)
    if not is_image_generation_model(cfg):
        kind = cfg.get("model_kind") or "chat"
        raise ImageGenerationConfigError(
            f"Model '{resolved}' has model_kind '{kind}', not '{IMAGE_GENERATION_TASK}'."
        )
    if not supports_task_capability(cfg, IMAGE_GENERATION_TASK):
        raise ImageGenerationConfigError(
            f"Model '{resolved}' does not advertise task_capabilities including '{IMAGE_GENERATION_TASK}'."
        )
    api_type = _normalized_api_type(cfg.get("api_type"))
    if api_type != OPENAI_IMAGES_API_TYPE:
        raise ImageGenerationConfigError(
            f"Model '{resolved}' has api_type '{api_type or 'chat_completions'}', not '{OPENAI_IMAGES_API_TYPE}'."
        )

    provider_name = cfg.get("provider")
    if not isinstance(provider_name, str) or not provider_name.strip():
        raise ImageGenerationConfigError(f"Model '{resolved}' has no provider.")
    provider_name = provider_name.strip()
    provider_config = dict(registry.provider_config(provider_name))
    if not provider_config:
        raise ImageGenerationConfigError(f"Provider '{provider_name}' not found for model '{resolved}'.")

    model_name = str(cfg.get("model_name") or "").strip()
    if not model_name:
        raise ImageGenerationConfigError(f"Model '{resolved}' has no model_name.")
    api_base = cfg.get("api_base") or provider_config.get("api_base")
    return OpenAIImagesBackend(
        model_key=resolved,
        provider_name=provider_name,
        provider_config=provider_config,
        model_config=dict(cfg),
        model_name=model_name,
        url=_resolve_openai_images_url(api_base),
    )


def _mime_type_for_output_format(output_format: Any) -> str:
    token = str(output_format or "").strip().lower().lstrip(".")
    return _OUTPUT_FORMAT_MIME_TYPES.get(token) or _DEFAULT_IMAGE_MIME_TYPE


def _extension_for_mime_type(mime_type: Any) -> str:
    token = str(mime_type or "").split(";", 1)[0].strip().lower()
    return _MIME_TYPE_EXTENSIONS.get(token) or "bin"


def _decode_b64_image(value: Any) -> tuple[bytes, str | None]:
    if not isinstance(value, str) or not value.strip():
        raise ImageGenerationProviderError("OpenAI Images response contains an empty b64_json field.")
    raw = value.strip()
    data_url_mime: str | None = None
    if raw.startswith("data:"):
        header, separator, encoded = raw.partition(",")
        if not separator:
            raise ImageGenerationProviderError("OpenAI Images data URL is missing a comma separator.")
        mime = header[5:].split(";", 1)[0].strip().lower()
        data_url_mime = mime or None
        raw = encoded
    try:
        return base64.b64decode(raw, validate=True), data_url_mime
    except Exception as e:  # pragma: no cover - exact binascii type varies by Python
        raise ImageGenerationProviderError("OpenAI Images response contains invalid base64 image data.") from e


def _response_json(response: Any) -> Mapping[str, Any]:
    try:
        body = response.json()
    except Exception as e:
        raise ImageGenerationProviderError("OpenAI Images response was not valid JSON.") from e
    if not isinstance(body, Mapping):
        raise ImageGenerationProviderError("OpenAI Images response JSON must be an object.")
    return body


def _download_url_image(session: Any, url: str, *, timeout: int) -> tuple[bytes, str | None]:
    if not hasattr(session, "get"):
        raise ImageGenerationProviderError("OpenAI Images URL response requires a session with get().")
    response = session.get(url, timeout=timeout)
    response.raise_for_status()
    data = getattr(response, "content", None)
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise ImageGenerationProviderError("Downloaded OpenAI Images URL response did not contain bytes.")
    headers = getattr(response, "headers", {}) or {}
    content_type = None
    if isinstance(headers, Mapping):
        content_type = headers.get("Content-Type") or headers.get("content-type")
    mime_type = str(content_type).split(";", 1)[0].strip().lower() if content_type else None
    return bytes(data), mime_type or None


def _parse_openai_images_response(
    body: Mapping[str, Any],
    *,
    session: Any,
    timeout: int,
    backend: OpenAIImagesBackend,
    request_options: Mapping[str, Any],
) -> tuple[dict[str, Any], tuple[GeneratedImage, ...]]:
    data_items = body.get("data")
    if not isinstance(data_items, list) or not data_items:
        raise ImageGenerationProviderError("OpenAI Images response did not contain any data items.")

    response_metadata: dict[str, Any] = {}
    for key in ("id", "created", "usage"):
        value = body.get(key)
        if value is not None:
            response_metadata[key] = value

    images: list[GeneratedImage] = []
    default_mime_type = _mime_type_for_output_format(request_options.get("output_format"))
    for index, item in enumerate(data_items):
        if not isinstance(item, Mapping):
            raise ImageGenerationProviderError("OpenAI Images data item must be an object.")
        item_mime_type = None
        source = "b64_json"
        source_url = None
        if item.get("b64_json"):
            image_bytes, item_mime_type = _decode_b64_image(item.get("b64_json"))
        elif item.get("url"):
            source = "url"
            source_url = str(item.get("url") or "").strip()
            if not source_url:
                raise ImageGenerationProviderError("OpenAI Images response contains an empty image URL.")
            image_bytes, item_mime_type = _download_url_image(session, source_url, timeout=timeout)
        else:
            raise ImageGenerationProviderError("OpenAI Images data item has neither b64_json nor url.")

        explicit_mime_type = item.get("mime_type") or item.get("content_type")
        if explicit_mime_type:
            item_mime_type = str(explicit_mime_type).split(";", 1)[0].strip().lower()
        mime_type = item_mime_type or default_mime_type
        filename = f"generated-{index + 1}.{_extension_for_mime_type(mime_type)}"
        metadata: dict[str, Any] = {
            "api_type": OPENAI_IMAGES_API_TYPE,
            "provider": backend.provider_name,
            "model_key": backend.model_key,
            "model": backend.model_name,
            "output_index": index,
            "source": source,
            "mime_type": mime_type,
            "filename": filename,
        }
        revised_prompt = item.get("revised_prompt")
        if isinstance(revised_prompt, str) and revised_prompt:
            metadata["revised_prompt"] = revised_prompt
        for key in ("id", "created"):
            value = body.get(key)
            if value is not None:
                metadata[f"response_{key}"] = value
        if source_url:
            metadata["source_url"] = source_url
        if request_options:
            metadata["request_options"] = dict(request_options)
        images.append(GeneratedImage(data=image_bytes, metadata=metadata))

    return response_metadata, tuple(images)


def generate_openai_images(
    prompt: str,
    *,
    model_key: str | None = None,
    models_path: str | Path = "models.json",
    all_models_path: str | Path = "all-models.json",
    registry: ModelRegistry | None = None,
    options: Mapping[str, Any] | None = None,
    timeout: int = 600,
    session: Any = None,
) -> ImageGenerationResult:
    """Generate images through a configured OpenAI Images backend.

    ``model_key`` may name a configured image backend.  If omitted, discovery is
    by ``task_capabilities: image_generation`` and ``model_kind:
    image_generation``.  Only the explicit Images API backend type is accepted;
    this function does not register or select a normal chat adapter.
    """

    prompt_text = str(prompt or "").strip()
    if not prompt_text:
        raise ValueError("image generation prompt must not be empty")

    registry = registry or _load_registry(models_path, all_models_path)
    backend = resolve_openai_images_backend(model_key, registry=registry)
    configured_options = _filter_openai_images_options(
        registry.merge_parameters(backend.model_key),
        reject_unknown=False,
    )
    explicit_options = _filter_openai_images_options(options, reject_unknown=True)
    request_options = {**configured_options, **explicit_options}
    payload: dict[str, Any] = {
        "model": backend.model_name,
        "prompt": prompt_text,
        **request_options,
    }

    headers = build_provider_headers(backend.provider_name, backend.provider_config, accept_sse=False)
    sess = session or requests
    response = sess.post(backend.url, headers=headers, json=payload, timeout=timeout)
    response.raise_for_status()
    response_metadata, images = _parse_openai_images_response(
        _response_json(response),
        session=sess,
        timeout=timeout,
        backend=backend,
        request_options=request_options,
    )
    return ImageGenerationResult(
        model_key=backend.model_key,
        provider_name=backend.provider_name,
        model_name=backend.model_name,
        prompt=prompt_text,
        request_options=request_options,
        response_metadata=response_metadata,
        images=images,
    )


__all__ = [
    "GeneratedImage",
    "IMAGE_GENERATION_TASK",
    "ImageGenerationConfigError",
    "ImageGenerationError",
    "ImageGenerationProviderError",
    "ImageGenerationResult",
    "OPENAI_IMAGES_API_TYPE",
    "OPENAI_IMAGES_OPTION_KEYS",
    "OpenAIImagesBackend",
    "generate_openai_images",
    "resolve_openai_images_backend",
]
