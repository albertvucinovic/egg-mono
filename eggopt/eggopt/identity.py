from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any


def canonical_json(value: Any, *, what: str) -> str:
    """Return a stable JSON identity or reject values that cannot be durable."""

    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{what} must contain only finite JSON values") from exc


def canonical_candidate(candidate: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    if not isinstance(candidate, Mapping) or not candidate:
        raise TypeError("candidate must be a non-empty mapping of strings to strings")
    normalized: list[tuple[str, str]] = []
    for name, text in candidate.items():
        if not isinstance(name, str) or not name:
            raise TypeError("candidate component names must be non-empty strings")
        if not isinstance(text, str):
            raise TypeError(f"candidate component {name!r} must be a string")
        normalized.append((name, text))
    return tuple(sorted(normalized))


def digest_payload(namespace: str, payload: Any) -> str:
    body = canonical_json(payload, what=f"{namespace} identity")
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return f"{namespace}:{digest}"
