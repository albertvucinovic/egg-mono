"""Tool call ID normalization for provider-specific constraints.

Some providers have specific requirements for tool_call_id format.
This module provides normalization strategies to transform IDs before
sending them to providers.

Strategies:
    - "mistral9": Normalize to exactly 9 alphanumeric chars (a-z, A-Z, 0-9)
                  Required by Mistral API which generates 32-char IDs but
                  validates for 9-char format.
"""
from __future__ import annotations

import hashlib
from typing import Optional


def normalize_tool_call_id(original_id: str, strategy: Optional[str]) -> str:
    """Normalize a tool_call_id based on provider strategy.

    Args:
        original_id: The original tool_call_id from the provider or internal generation
        strategy: Normalization strategy name (e.g., "mistral9") or None for passthrough

    Returns:
        Normalized tool_call_id meeting provider constraints, or original if no strategy
    """
    if not strategy or not original_id:
        return original_id

    if strategy == "mistral9":
        return _normalize_mistral9(original_id)

    # Unknown strategy, pass through unchanged
    return original_id


def _normalize_mistral9(original_id: str) -> str:
    """Normalize to Mistral's required format: exactly 9 alphanumeric chars.

    Mistral API bug: Their streaming generates 32-char mixed-case IDs,
    but their validation requires exactly 9 alphanumeric characters (a-z, A-Z, 0-9).

    Uses SHA-256 hash and base-62 encoding for deterministic transformation.
    Same input always produces same output.
    """
    # Already valid - pass through
    if len(original_id) == 9 and original_id.isalnum():
        return original_id

    # Hash the original ID for deterministic transformation
    h = hashlib.sha256(original_id.encode('utf-8')).digest()

    # Encode to base-62 (alphanumeric: 0-9, A-Z, a-z)
    charset = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    val = int.from_bytes(h[:8], 'big')  # Use first 8 bytes (64 bits)

    result = []
    for _ in range(9):
        result.append(charset[val % 62])
        val //= 62

    return ''.join(result)
