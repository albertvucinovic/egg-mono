from __future__ import annotations

"""Provider-backed image generation helpers.

This module intentionally stays out of the normal chat adapter registry.  Image
backends are loaded from the dedicated ``image-generation-models.json`` config
and reuse provider credentials/base URLs from ``models.json``.
"""

import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import requests

from .capabilities import is_image_generation_model, supports_task_capability
from .catalog import AllModelsCatalog
from .config import load_image_generation_models_config
from .provider_http import build_provider_headers
from .registry import ModelRegistry

IMAGE_GENERATION_TASK = "image_generation"
OPENAI_IMAGES_API_TYPE = "openai_images"
CODEX_IMAGES_API_TYPE = "codex_images"
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
CODEX_IMAGES_OPTION_KEYS = frozenset(
    {
        "background",
        "quality",
        "size",
    }
)
SUPPORTED_IMAGE_GENERATION_API_TYPES = frozenset(
    {
        OPENAI_IMAGES_API_TYPE,
        CODEX_IMAGES_API_TYPE,
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
class CodexImagesBackend:
    """Resolved Codex-style image-generation backend configuration."""

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


def _load_registry(
    models_path: str | Path,
    all_models_path: str | Path,
    image_generation_models_path: str | Path | None = None,
) -> ModelRegistry:
    models_config, providers_config = load_image_generation_models_config(
        image_generation_models_path,
        models_path=models_path,
    )
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


def _filter_codex_images_options(
    options: Mapping[str, Any] | None,
    *,
    reject_unknown: bool,
) -> dict[str, Any]:
    if not isinstance(options, Mapping):
        return {}
    unknown = sorted(
        str(key)
        for key, value in options.items()
        if value is not None and str(key) not in CODEX_IMAGES_OPTION_KEYS and str(key) not in {"n", "output_format"}
    )
    n_value = options.get("n")
    if n_value is not None:
        try:
            n_int = int(n_value)
        except (TypeError, ValueError):
            unknown.append("n")
        else:
            if isinstance(n_value, bool) or n_int != 1:
                if reject_unknown:
                    raise ImageGenerationConfigError(
                        "Codex image generation currently supports one image per call; "
                        "omit n or use n=1, or choose an openai_images backend for multi-image generation."
                    )
            # n=1 is the default/single-image case for this backend.  The
            # Codex image endpoint supports an optional n field in its typed
            # request, but Codex's own imagegen extension omits it for
            # standalone generation.  Keep Egg's request Codex-like and drop a
            # harmless n=1 from generic generate_image calls.
    output_format = options.get("output_format")
    if output_format is not None:
        fmt = str(output_format).strip().lower().lstrip(".")
        if fmt == "jpg":
            fmt = "jpeg"
        if fmt and fmt != "png" and reject_unknown:
            raise ImageGenerationConfigError(
                "Codex image generation currently returns PNG; omit output_format or use output_format=png."
            )
        # Codex's typed request does not include output_format.  Drop the
        # harmless PNG/default value so generic generate_image calls can work
        # across both OpenAI Images and Codex-style backends.
    if unknown and reject_unknown:
        joined = ", ".join(unknown)
        raise ImageGenerationConfigError(f"Unsupported Codex image generation option(s): {joined}")
    return {
        str(key): value
        for key, value in options.items()
        if value is not None and str(key) in CODEX_IMAGES_OPTION_KEYS
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


def _resolve_codex_images_url(api_base: Any) -> str:
    base = str(api_base or "").strip()
    if not base:
        raise ImageGenerationConfigError("Codex image provider is missing api_base.")
    stripped = base.rstrip("/")
    if stripped.endswith(OPENAI_IMAGES_GENERATIONS_PATH):
        return stripped
    if stripped.endswith("/images"):
        return stripped + "/generations"
    for suffix in ("/responses", "/chat/completions"):
        if stripped.endswith(suffix):
            return stripped[: -len(suffix)] + OPENAI_IMAGES_GENERATIONS_PATH
    return stripped + OPENAI_IMAGES_GENERATIONS_PATH


def _validate_image_generation_backend(
    registry: ModelRegistry,
    resolved: str,
    *,
    expected_api_type: str,
) -> tuple[dict[str, Any], str, dict[str, Any], str]:
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
    if api_type != expected_api_type:
        raise ImageGenerationConfigError(
            f"Model '{resolved}' has api_type '{api_type or 'chat_completions'}', not '{expected_api_type}'."
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
    return dict(cfg), provider_name, provider_config, model_name


def _configured_backend_candidates(
    registry: ModelRegistry,
    *,
    expected_api_type: str | None = None,
) -> list[str]:
    candidates: list[str] = []
    default = registry.default_model_key()
    if isinstance(default, str) and default.strip():
        resolved_default = _resolve_listed_image_generation_model(registry, default)
        candidates.append(resolved_default)

    for candidate in registry.task_model_keys(IMAGE_GENERATION_TASK, model_kind=IMAGE_GENERATION_TASK):
        if candidate not in candidates:
            candidates.append(candidate)

    if expected_api_type is None:
        return candidates
    return [
        candidate
        for candidate in candidates
        if _normalized_api_type(registry.get_effective_model_config(candidate).get("api_type")) == expected_api_type
    ]


def _resolve_listed_image_generation_model(registry: ModelRegistry, model_key: str) -> str:
    if str(model_key or "").strip().lower().startswith("all:"):
        raise ImageGenerationConfigError(
            "Image generation models must be listed in image-generation-models.json; "
            "catalog/all: model handles are only for normal chat model selection."
        )
    resolved = registry.resolve(model_key)
    if not resolved:
        raise ImageGenerationConfigError(f"Unknown image generation model: {model_key}")
    return resolved


def resolve_openai_images_backend(
    model_key: str | None = None,
    *,
    registry: ModelRegistry | None = None,
    models_path: str | Path = "models.json",
    all_models_path: str | Path = "all-models.json",
    image_generation_models_path: str | Path | None = None,
) -> OpenAIImagesBackend:
    """Resolve and validate an ``api_type: openai_images`` backend.

    If ``model_key`` is omitted, the first configured backend advertising the
    ``image_generation`` task and ``image_generation`` model kind is selected.
    The normal chat model selection is not changed.
    """

    registry = registry or _load_registry(models_path, all_models_path, image_generation_models_path)
    if model_key:
        resolved = _resolve_listed_image_generation_model(registry, model_key)
    else:
        candidates = _configured_backend_candidates(registry, expected_api_type=OPENAI_IMAGES_API_TYPE)
        resolved = candidates[0] if candidates else None
        if not resolved:
            raise ImageGenerationConfigError(
                "No api_type: openai_images image generation model is configured in image-generation-models.json."
            )

    cfg, provider_name, provider_config, model_name = _validate_image_generation_backend(
        registry,
        resolved,
        expected_api_type=OPENAI_IMAGES_API_TYPE,
    )
    api_base = cfg.get("api_base") or provider_config.get("api_base")
    return OpenAIImagesBackend(
        model_key=resolved,
        provider_name=provider_name,
        provider_config=provider_config,
        model_config=dict(cfg),
        model_name=model_name,
        url=_resolve_openai_images_url(api_base),
    )


def resolve_codex_images_backend(
    model_key: str | None = None,
    *,
    registry: ModelRegistry | None = None,
    models_path: str | Path = "models.json",
    all_models_path: str | Path = "all-models.json",
    image_generation_models_path: str | Path | None = None,
) -> CodexImagesBackend:
    """Resolve and validate an ``api_type: codex_images`` backend.

    This is the Codex-style image endpoint used by Codex's standalone
    image-generation extension.  It posts directly to ``images/generations``
    relative to the configured provider base URL instead of trying to force a
    provider-native image-generation tool into a normal Responses call.
    """

    registry = registry or _load_registry(models_path, all_models_path, image_generation_models_path)
    if model_key:
        resolved = _resolve_listed_image_generation_model(registry, model_key)
    else:
        candidates = _configured_backend_candidates(registry, expected_api_type=CODEX_IMAGES_API_TYPE)
        resolved = candidates[0] if candidates else None
        if not resolved:
            raise ImageGenerationConfigError(
                "No api_type: codex_images image generation model is configured in image-generation-models.json."
            )

    cfg, provider_name, provider_config, model_name = _validate_image_generation_backend(
        registry,
        resolved,
        expected_api_type=CODEX_IMAGES_API_TYPE,
    )
    api_base = cfg.get("api_base") or provider_config.get("api_base")
    return CodexImagesBackend(
        model_key=resolved,
        provider_name=provider_name,
        provider_config=provider_config,
        model_config=dict(cfg),
        model_name=model_name,
        url=_resolve_codex_images_url(api_base),
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


def _response_json(response: Any, *, label: str = "OpenAI Images") -> Mapping[str, Any]:
    try:
        body = response.json()
    except Exception as e:
        raise ImageGenerationProviderError(f"{label} response was not valid JSON.") from e
    if not isinstance(body, Mapping):
        raise ImageGenerationProviderError(f"{label} response JSON must be an object.")
    return body


def _response_body_preview(response: Any, *, limit: int = 2000) -> str:
    body = getattr(response, "text", None)
    if not isinstance(body, str) or not body.strip():
        content = getattr(response, "content", None)
        if isinstance(content, (bytes, bytearray, memoryview)):
            try:
                body = bytes(content).decode("utf-8", errors="replace")
            except Exception:
                body = repr(bytes(content)[:limit])
    if not isinstance(body, str) or not body.strip():
        try:
            parsed = response.json()
        except Exception:
            return ""
        try:
            import json as _json

            body = _json.dumps(parsed, ensure_ascii=False, sort_keys=True)
        except Exception:
            body = repr(parsed)
    body = " ".join(body.split())
    if len(body) > limit:
        body = body[: max(0, limit - 1)].rstrip() + "…"
    return body


def _raise_for_status(response: Any, *, label: str) -> None:
    try:
        response.raise_for_status()
    except Exception as e:
        body = _response_body_preview(response)
        suffix = f" Response body: {body}" if body else ""
        raise ImageGenerationProviderError(f"{label} request failed: {e}.{suffix}") from e


def _download_url_image(session: Any, url: str, *, timeout: int) -> tuple[bytes, str | None]:
    if not hasattr(session, "get"):
        raise ImageGenerationProviderError("OpenAI Images URL response requires a session with get().")
    response = session.get(url, timeout=timeout)
    _raise_for_status(response, label="OpenAI Images URL download")
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


def _provider_error_message(body: Mapping[str, Any], *, label: str) -> str | None:
    error = body.get("error")
    if not error:
        return None
    if isinstance(error, Mapping):
        code = error.get("code") or error.get("type") or "unknown"
        message = error.get("message") or str(error)
        return f"{label} error ({code}): {message}"
    return f"{label} error: {error}"


def _parse_codex_images_response(
    body: Mapping[str, Any],
    *,
    backend: CodexImagesBackend,
    request_options: Mapping[str, Any],
) -> tuple[dict[str, Any], tuple[GeneratedImage, ...]]:
    provider_error = _provider_error_message(body, label="Codex images")
    if provider_error:
        raise ImageGenerationProviderError(provider_error)

    data_items = body.get("data")
    if not isinstance(data_items, list) or not data_items:
        raise ImageGenerationProviderError("Codex images response did not contain any data items.")

    response_metadata: dict[str, Any] = {"api_type": CODEX_IMAGES_API_TYPE}
    for key in ("id", "created", "created_at", "background", "quality", "size", "usage"):
        value = body.get(key)
        if value is not None:
            response_metadata[key] = value

    images: list[GeneratedImage] = []
    default_mime_type = _DEFAULT_IMAGE_MIME_TYPE
    response_id = body.get("id")
    response_created = body.get("created") if body.get("created") is not None else body.get("created_at")
    for index, item in enumerate(data_items):
        if not isinstance(item, Mapping):
            raise ImageGenerationProviderError("Codex images data item must be an object.")
        if not item.get("b64_json"):
            raise ImageGenerationProviderError("Codex images data item has no b64_json field.")
        image_bytes, data_url_mime = _decode_b64_image(item.get("b64_json"))
        explicit_mime_type = item.get("mime_type") or item.get("content_type")
        mime_type = (
            str(explicit_mime_type).split(";", 1)[0].strip().lower()
            if explicit_mime_type
            else data_url_mime or default_mime_type
        )
        filename = f"generated-{index + 1}.{_extension_for_mime_type(mime_type)}"
        metadata: dict[str, Any] = {
            "api_type": CODEX_IMAGES_API_TYPE,
            "provider": backend.provider_name,
            "model_key": backend.model_key,
            "model": backend.model_name,
            "output_index": index,
            "source": "b64_json",
            "mime_type": mime_type,
            "filename": filename,
        }
        revised_prompt = item.get("revised_prompt")
        if isinstance(revised_prompt, str) and revised_prompt:
            metadata["revised_prompt"] = revised_prompt
        if response_id is not None:
            metadata["response_id"] = response_id
        if response_created is not None:
            metadata["response_created"] = response_created
        if request_options:
            metadata["request_options"] = dict(request_options)
        images.append(GeneratedImage(data=image_bytes, metadata=metadata))

    return response_metadata, tuple(images)


def _select_image_generation_api_type(
    model_key: str | None,
    registry: ModelRegistry,
) -> tuple[str | None, str]:
    if model_key:
        resolved = _resolve_listed_image_generation_model(registry, model_key)
        api_type = _normalized_api_type(registry.get_effective_model_config(resolved).get("api_type"))
        if api_type not in SUPPORTED_IMAGE_GENERATION_API_TYPES:
            supported = ", ".join(sorted(SUPPORTED_IMAGE_GENERATION_API_TYPES))
            raise ImageGenerationConfigError(
                f"Model '{resolved}' has unsupported image generation api_type '{api_type or 'chat_completions'}'. "
                f"Supported types: {supported}."
            )
        return resolved, api_type

    for candidate in _configured_backend_candidates(registry):
        api_type = _normalized_api_type(registry.get_effective_model_config(candidate).get("api_type"))
        if api_type in SUPPORTED_IMAGE_GENERATION_API_TYPES:
            return candidate, api_type
    supported = ", ".join(sorted(SUPPORTED_IMAGE_GENERATION_API_TYPES))
    raise ImageGenerationConfigError(
        f"No supported image generation backend is configured. Supported api_type values: {supported}."
    )


def generate_openai_images(
    prompt: str,
    *,
    model_key: str | None = None,
    models_path: str | Path = "models.json",
    all_models_path: str | Path = "all-models.json",
    image_generation_models_path: str | Path | None = None,
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

    registry = registry or _load_registry(models_path, all_models_path, image_generation_models_path)
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

    headers = build_provider_headers(
        backend.provider_name,
        backend.provider_config,
        accept_sse=False,
        include_responses_beta=False,
    )
    sess = session or requests
    response = sess.post(backend.url, headers=headers, json=payload, timeout=timeout)
    _raise_for_status(response, label="OpenAI Images")
    response_metadata, images = _parse_openai_images_response(
        _response_json(response, label="OpenAI Images"),
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


def generate_codex_images(
    prompt: str,
    *,
    model_key: str | None = None,
    models_path: str | Path = "models.json",
    all_models_path: str | Path = "all-models.json",
    image_generation_models_path: str | Path | None = None,
    registry: ModelRegistry | None = None,
    options: Mapping[str, Any] | None = None,
    timeout: int = 600,
    session: Any = None,
) -> ImageGenerationResult:
    """Generate images through a configured Codex-style image backend.

    Codex's standalone image-generation extension posts typed requests to
    ``images/generations`` relative to the Codex provider base URL.  Egg uses
    the same direct image endpoint shape here instead of routing generation
    through a normal Responses turn with a forced ``image_generation`` tool.
    """

    prompt_text = str(prompt or "").strip()
    if not prompt_text:
        raise ValueError("image generation prompt must not be empty")

    registry = registry or _load_registry(models_path, all_models_path, image_generation_models_path)
    backend = resolve_codex_images_backend(model_key, registry=registry)

    configured_options = _filter_codex_images_options(
        registry.merge_parameters(backend.model_key),
        reject_unknown=False,
    )
    explicit_options = _filter_codex_images_options(options, reject_unknown=True)
    request_options = {**configured_options, **explicit_options}
    payload: dict[str, Any] = {
        "prompt": prompt_text,
        "model": backend.model_name,
        **request_options,
    }

    headers = build_provider_headers(
        backend.provider_name,
        backend.provider_config,
        accept_sse=False,
        include_responses_beta=False,
    )
    sess = session or requests
    response = sess.post(backend.url, headers=headers, json=payload, timeout=timeout)
    _raise_for_status(response, label="Codex images")
    response_metadata, images = _parse_codex_images_response(
        _response_json(response, label="Codex images"),
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


def generate_images(
    prompt: str,
    *,
    model_key: str | None = None,
    models_path: str | Path = "models.json",
    all_models_path: str | Path = "all-models.json",
    image_generation_models_path: str | Path | None = None,
    registry: ModelRegistry | None = None,
    options: Mapping[str, Any] | None = None,
    timeout: int = 600,
    session: Any = None,
) -> ImageGenerationResult:
    """Generate images through any configured, supported image backend."""

    registry = registry or _load_registry(models_path, all_models_path, image_generation_models_path)
    resolved_model_key, api_type = _select_image_generation_api_type(model_key, registry)
    if api_type == OPENAI_IMAGES_API_TYPE:
        return generate_openai_images(
            prompt,
            model_key=resolved_model_key or model_key,
            registry=registry,
            options=options,
            timeout=timeout,
            session=session,
        )
    if api_type == CODEX_IMAGES_API_TYPE:
        return generate_codex_images(
            prompt,
            model_key=resolved_model_key or model_key,
            registry=registry,
            options=options,
            timeout=timeout,
            session=session,
        )
    raise ImageGenerationConfigError(f"Unsupported image generation api_type: {api_type}")


__all__ = [
    "GeneratedImage",
    "IMAGE_GENERATION_TASK",
    "ImageGenerationConfigError",
    "ImageGenerationError",
    "ImageGenerationProviderError",
    "ImageGenerationResult",
    "CODEX_IMAGES_API_TYPE",
    "CODEX_IMAGES_OPTION_KEYS",
    "OPENAI_IMAGES_API_TYPE",
    "OPENAI_IMAGES_OPTION_KEYS",
    "CodexImagesBackend",
    "OpenAIImagesBackend",
    "SUPPORTED_IMAGE_GENERATION_API_TYPES",
    "generate_codex_images",
    "generate_images",
    "generate_openai_images",
    "resolve_codex_images_backend",
    "resolve_openai_images_backend",
]
