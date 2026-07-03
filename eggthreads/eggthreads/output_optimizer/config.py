from __future__ import annotations

"""Configuration helpers for the native output optimizer gate."""

import os
from typing import Any, Mapping

OUTPUT_OPTIMIZER_ENV = "EGG_OUTPUT_OPTIMIZER"
_TRUE_LIKE_VALUES = frozenset({"1", "true", "on", "yes"})
_CONFIG_KEYS = (
    "output_optimizer_enabled",
    "native_output_optimizer_enabled",
    "egg_output_optimizer",
)


def is_truthy_output_optimizer_flag(value: Any) -> bool:
    """Return True for explicit true-like optimizer flag values."""

    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in _TRUE_LIKE_VALUES


def output_optimizer_enabled(
    config: Mapping[str, Any] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Return whether the native optimizer is explicitly enabled.

    The gate is disabled by default.  For this Phase-2 integration slice it can
    be enabled either by an in-process config mapping or by setting
    ``EGG_OUTPUT_OPTIMIZER`` to one of ``1``, ``true``, ``on``, or ``yes``.
    """

    if config:
        for key in _CONFIG_KEYS:
            if key in config:
                return is_truthy_output_optimizer_flag(config.get(key))

    env = os.environ if environ is None else environ
    return is_truthy_output_optimizer_flag(env.get(OUTPUT_OPTIMIZER_ENV) if env is not None else None)


__all__ = [
    "OUTPUT_OPTIMIZER_ENV",
    "is_truthy_output_optimizer_flag",
    "output_optimizer_enabled",
]
