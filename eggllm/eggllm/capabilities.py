from __future__ import annotations

"""Model capability metadata helpers.

The models.json schema is intentionally permissive.  These helpers provide a
stable normalized view for chat selection and attachment lowering while keeping
old model entries backwards compatible.
"""

from collections.abc import Mapping
from typing import Any, Dict, List

DEFAULT_MODEL_KIND = "chat"
DEFAULT_INPUT_MODALITIES = ["text", "image"]
DEFAULT_OUTPUT_MODALITIES = ["text"]
DEFAULT_TASK_CAPABILITIES = ["chat"]
DEFAULT_ATTACHMENT_CAPABILITIES: Dict[str, Any] = {}
DEFAULT_IMAGE_MIME_TYPES = ["image/png", "image/jpeg", "image/gif", "image/webp"]
_MIME_TYPE_KEYS = ("mime_types", "mimes", "media_types", "supported_mime_types")
_CAPABILITY_ENABLE_KEYS = (
    "mime_types",
    "mimes",
    "media_types",
    "supported_mime_types",
    "max_size_bytes",
    "max_file_bytes",
    "max_image_bytes",
    "max_request_attachment_bytes",
    "detail",
    "supports_inline_base64",
    "supports_provider_file_id",
    "supports_file_url",
    "supports_image_detail",
)
CAPABILITY_METADATA_KEYS = (
    "input_modalities",
    "output_modalities",
    "model_kind",
    "task_capabilities",
    "attachment_capabilities",
)
PROVIDER_MODEL_DEFAULT_KEYS = (*CAPABILITY_METADATA_KEYS, "api_type")


def _clean_token(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def _string_list(value: Any, default: List[str]) -> List[str]:
    if value is None:
        return list(default)
    raw: list[Any]
    if isinstance(value, str):
        raw = [value]
    elif isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        return list(default)
    out: List[str] = []
    for item in raw:
        token = _clean_token(item)
        if token and token not in out:
            out.append(token)
    return out or list(default)


def model_kind(model_config: Mapping[str, Any] | None) -> str:
    """Return the normalized model kind, defaulting old entries to ``chat``."""

    if not isinstance(model_config, Mapping):
        return DEFAULT_MODEL_KIND
    kind = _clean_token(model_config.get("model_kind"))
    return kind or DEFAULT_MODEL_KIND


def input_modalities(model_config: Mapping[str, Any] | None) -> List[str]:
    if not isinstance(model_config, Mapping):
        return list(DEFAULT_INPUT_MODALITIES)
    return _string_list(model_config.get("input_modalities"), DEFAULT_INPUT_MODALITIES)


def output_modalities(model_config: Mapping[str, Any] | None) -> List[str]:
    if not isinstance(model_config, Mapping):
        return list(DEFAULT_OUTPUT_MODALITIES)
    return _string_list(model_config.get("output_modalities"), DEFAULT_OUTPUT_MODALITIES)


def task_capabilities(model_config: Mapping[str, Any] | None) -> List[str]:
    if not isinstance(model_config, Mapping):
        return list(DEFAULT_TASK_CAPABILITIES)
    kind = model_kind(model_config)
    default = DEFAULT_TASK_CAPABILITIES if kind == DEFAULT_MODEL_KIND else [kind]
    return _string_list(model_config.get("task_capabilities"), default)


def attachment_capabilities(model_config: Mapping[str, Any] | None) -> Dict[str, Any]:
    if not isinstance(model_config, Mapping):
        return dict(DEFAULT_ATTACHMENT_CAPABILITIES)
    caps = model_config.get("attachment_capabilities")
    return dict(caps) if isinstance(caps, Mapping) else dict(DEFAULT_ATTACHMENT_CAPABILITIES)


def model_metadata(model_config: Mapping[str, Any] | None) -> Dict[str, Any]:
    """Return normalized capability metadata with backward-compatible defaults."""

    return {
        "model_kind": model_kind(model_config),
        "input_modalities": input_modalities(model_config),
        "output_modalities": output_modalities(model_config),
        "task_capabilities": task_capabilities(model_config),
        "attachment_capabilities": attachment_capabilities(model_config),
    }


def effective_model_config(
    provider_config: Mapping[str, Any] | None,
    model_config: Mapping[str, Any] | None,
) -> Dict[str, Any]:
    """Return model config with provider-level capability metadata defaults.

    Provider-level metadata is useful when all models under a provider share an
    API modality/capability policy.  Per-model keys still take precedence.
    """

    def merge_mapping(base: Any, override: Any) -> Dict[str, Any]:
        out_map = dict(base) if isinstance(base, Mapping) else {}
        if not isinstance(override, Mapping):
            return out_map
        for key, value in override.items():
            if isinstance(value, Mapping) and isinstance(out_map.get(key), Mapping):
                out_map[key] = merge_mapping(out_map[key], value)
            else:
                out_map[key] = value
        return out_map

    out: Dict[str, Any] = {}
    if isinstance(provider_config, Mapping):
        for key in PROVIDER_MODEL_DEFAULT_KEYS:
            if key in provider_config:
                out[key] = provider_config[key]
    if isinstance(model_config, Mapping):
        for key, value in model_config.items():
            if key == "attachment_capabilities" and key in out:
                out[key] = merge_mapping(out[key], value)
            else:
                out[key] = value
        # A model-level modality list is an explicit statement about that
        # model.  If a provider enables images by default but one model says
        # it only accepts text, do not let the provider image default leak
        # through unless the model also explicitly re-enables images in its
        # own attachment_capabilities.
        if "input_modalities" in model_config:
            explicit_modalities = _string_list(model_config.get("input_modalities"), [])
            model_caps = model_config.get("attachment_capabilities")
            model_caps_has_image = isinstance(model_caps, Mapping) and _capability_entry(model_caps, "image") is not None
            if "image" not in explicit_modalities and not model_caps_has_image:
                caps = dict(out.get("attachment_capabilities")) if isinstance(out.get("attachment_capabilities"), Mapping) else {}
                caps["images"] = False
                out["attachment_capabilities"] = caps
            elif "image" in explicit_modalities and not model_caps_has_image:
                caps = dict(out.get("attachment_capabilities")) if isinstance(out.get("attachment_capabilities"), Mapping) else {}
                if _entry_disabled(_capability_entry(caps, "image")):
                    caps["images"] = True
                    out["attachment_capabilities"] = caps
    return out


def is_model_kind(model_config: Mapping[str, Any] | None, kind: str) -> bool:
    token = _clean_token(kind)
    return bool(token) and model_kind(model_config) == token


def is_chat_model(model_config: Mapping[str, Any] | None) -> bool:
    return is_model_kind(model_config, DEFAULT_MODEL_KIND)


def is_image_generation_model(model_config: Mapping[str, Any] | None) -> bool:
    return is_model_kind(model_config, "image_generation")


def supports_task_capability(model_config: Mapping[str, Any] | None, task: str) -> bool:
    token = _clean_token(task)
    return bool(token) and token in task_capabilities(model_config)


def supports_input_modality(model_config: Mapping[str, Any] | None, modality: str) -> bool:
    token = _clean_token(modality)
    return token in input_modalities(model_config)


def _capability_entry(caps: Mapping[str, Any], presentation: str) -> Any:
    presentation = _clean_token(presentation)
    candidates = [presentation]
    if presentation == "image":
        candidates.append("images")
    elif presentation.endswith("s"):
        candidates.append(presentation[:-1])
    else:
        candidates.append(presentation + "s")
    for key in candidates:
        if key in caps:
            return caps.get(key)
    return None


def _entry_enabled(entry: Any) -> bool:
    if isinstance(entry, bool):
        return entry
    if isinstance(entry, Mapping):
        for key in ("enabled", "supported", "allow", "allowed"):
            if key in entry:
                return bool(entry.get(key))
        # A capability object with concrete constraints is an explicit allow.
        if any(key in entry for key in _CAPABILITY_ENABLE_KEYS):
            return True
    return False


def _entry_disabled(entry: Any) -> bool:
    """Return whether a capability entry is an explicit opt-out.

    Missing capability metadata now means "try the common image path" for
    OpenAI-compatible providers.  Explicit opt-outs therefore need to win over
    default input modalities and provider-level defaults.
    """

    if isinstance(entry, bool):
        return entry is False
    if isinstance(entry, Mapping):
        for key in ("enabled", "supported", "allow", "allowed"):
            if key in entry:
                return not bool(entry.get(key))
    return False


def _entry_mime_types(entry: Any, presentation: str, *, default_image: bool = True) -> List[str]:
    if isinstance(entry, Mapping):
        raw = None
        for key in _MIME_TYPE_KEYS:
            raw = entry.get(key)
            if raw is not None:
                break
        if raw is not None:
            return _string_list(raw, [])
    if default_image and _clean_token(presentation) == "image":
        return list(DEFAULT_IMAGE_MIME_TYPES)
    return []


def _mime_matches_presentation(mime_type: str, presentation: str) -> bool:
    mime = _clean_token(mime_type).replace("_", "-")
    presentation = _clean_token(presentation)
    if presentation == "image":
        return mime.startswith("image/")
    if presentation == "document":
        return mime == "application/pdf" or mime.startswith("text/") or mime.startswith("application/vnd.")
    if presentation == "file":
        return bool(mime)
    return False


def supports_attachment_presentation(
    model_config: Mapping[str, Any] | None,
    presentation: str,
    *,
    mime_type: str | None = None,
) -> bool:
    """Return whether a model explicitly supports an attachment presentation.

    Old model entries default to text-only and therefore return ``False`` for
    attachments.  A model may opt in either through ``input_modalities`` (for
    example ``["text", "image"]``) or through ``attachment_capabilities`` such
    as ``{"images": true}`` or ``{"image": {"mime_types": [...]}}``.
    """

    presentation_token = _clean_token(presentation)
    if not presentation_token:
        return False
    if mime_type and not _mime_matches_presentation(mime_type, presentation_token):
        return False
    caps = attachment_capabilities(model_config)
    entry = _capability_entry(caps, presentation_token)
    if _entry_disabled(entry):
        return False
    top_level_mimes = [
        mime
        for mime in _entry_mime_types(caps, presentation_token, default_image=False)
        if _mime_matches_presentation(mime, presentation_token)
    ]
    explicitly_allowed = (
        _entry_enabled(entry)
        or supports_input_modality(model_config, presentation_token)
        or any(_mime_matches_presentation(mime, presentation_token) for mime in top_level_mimes)
    )
    if not explicitly_allowed:
        return False
    if mime_type:
        entry_mimes = _entry_mime_types(entry, presentation_token, default_image=False)
        allowed_mimes = entry_mimes or top_level_mimes or _entry_mime_types(entry, presentation_token)
        if allowed_mimes and _clean_token(mime_type).replace("_", "-") not in [m.replace("_", "-") for m in allowed_mimes]:
            return False
    return True


__all__ = [
    "DEFAULT_ATTACHMENT_CAPABILITIES",
    "CAPABILITY_METADATA_KEYS",
    "PROVIDER_MODEL_DEFAULT_KEYS",
    "DEFAULT_IMAGE_MIME_TYPES",
    "DEFAULT_INPUT_MODALITIES",
    "DEFAULT_MODEL_KIND",
    "DEFAULT_OUTPUT_MODALITIES",
    "DEFAULT_TASK_CAPABILITIES",
    "attachment_capabilities",
    "effective_model_config",
    "input_modalities",
    "is_chat_model",
    "is_image_generation_model",
    "is_model_kind",
    "model_kind",
    "model_metadata",
    "output_modalities",
    "supports_attachment_presentation",
    "supports_input_modality",
    "supports_task_capability",
    "task_capabilities",
]
