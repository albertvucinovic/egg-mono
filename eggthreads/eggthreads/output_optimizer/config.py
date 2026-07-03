from __future__ import annotations

"""Configuration helpers for the native output optimizer gate.

The process/env gate from the Phase-2 integration remains the default path:
when no event-sourced per-thread config is present, ``EGG_OUTPUT_OPTIMIZER``
and explicit config mappings are interpreted exactly as before.

Per-thread control is stored as ``output_optimizer.config`` events.  Events are
field-wise/incremental and inherit through ancestors: a child can override just
``mode`` while inheriting an ancestor's explicit enabled/disabled state, or vice
versa.
"""

from dataclasses import dataclass, field
import json
import os
from types import MappingProxyType
from typing import Any, Mapping, Optional, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from ..db import ThreadsDB

OUTPUT_OPTIMIZER_ENV = "EGG_OUTPUT_OPTIMIZER"
OUTPUT_OPTIMIZER_RTK_ENV = "EGG_OUTPUT_OPTIMIZER_RTK"
OUTPUT_OPTIMIZER_RTK_BIN_ENV = "EGG_OUTPUT_OPTIMIZER_RTK_BIN"
OUTPUT_OPTIMIZER_RTK_TIMEOUT_ENV = "EGG_OUTPUT_OPTIMIZER_RTK_TIMEOUT"
OUTPUT_OPTIMIZER_RTK_PRIVACY_OPT_IN_ENV = "EGG_OUTPUT_OPTIMIZER_RTK_PRIVACY_OPT_IN"
OUTPUT_OPTIMIZER_CONFIG_EVENT_TYPE = "output_optimizer.config"
DEFAULT_OUTPUT_OPTIMIZER_MODE = "balanced"
DEFAULT_OUTPUT_OPTIMIZER_RTK_COMMAND = "rtk"
DEFAULT_OUTPUT_OPTIMIZER_RTK_TIMEOUT_SECONDS = 3.0
OUTPUT_OPTIMIZER_MODES = ("conservative", "balanced", "aggressive")
OUTPUT_OPTIMIZER_MODE_MIN_CONFIDENCE: Mapping[str, float] = MappingProxyType(
    {
        "conservative": 0.9,
        # Balanced intentionally matches the Phase-2/3 hard-coded policy value
        # so env-only/no-event behavior stays compatible.
        "balanced": 0.5,
        "aggressive": 0.0,
    }
)

_TRUE_LIKE_VALUES = frozenset({"1", "true", "on", "yes"})
_FALSE_LIKE_VALUES = frozenset({"0", "false", "off", "no"})
_CONFIG_KEYS = (
    "output_optimizer_enabled",
    "native_output_optimizer_enabled",
    "egg_output_optimizer",
)
_RTK_CONFIG_KEYS = (
    "output_optimizer_rtk_enabled",
    "native_output_optimizer_rtk_enabled",
    "egg_output_optimizer_rtk",
)
_RTK_COMMAND_CONFIG_KEYS = (
    "output_optimizer_rtk_command",
    "output_optimizer_rtk_binary",
    "native_output_optimizer_rtk_command",
    "native_output_optimizer_rtk_binary",
)
_RTK_TIMEOUT_CONFIG_KEYS = (
    "output_optimizer_rtk_timeout_seconds",
    "output_optimizer_rtk_timeout",
    "native_output_optimizer_rtk_timeout_seconds",
    "native_output_optimizer_rtk_timeout",
)
_RTK_PRIVACY_OPT_IN_CONFIG_KEYS = (
    "output_optimizer_rtk_privacy_opt_in",
    "native_output_optimizer_rtk_privacy_opt_in",
)
_EVENT_ENABLED_KEYS = ("enabled", *_CONFIG_KEYS)
_EVENT_MODE_KEYS = ("mode", "output_optimizer_mode", "native_output_optimizer_mode")


def is_truthy_output_optimizer_flag(value: Any) -> bool:
    """Return True for explicit true-like optimizer flag values."""

    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in _TRUE_LIKE_VALUES


def _coerce_optional_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return bool(value)
    text = str(value).strip().lower()
    if text in _TRUE_LIKE_VALUES:
        return True
    if text in _FALSE_LIKE_VALUES:
        return False
    return None


def output_optimizer_enabled(
    config: Mapping[str, Any] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Return whether the native optimizer is explicitly enabled.

    The gate is disabled by default.  It can be enabled either by an in-process
    config mapping or by setting ``EGG_OUTPUT_OPTIMIZER`` to one of ``1``,
    ``true``, ``on``, or ``yes``.  Existing mapping-key precedence is preserved:
    if one of the known mapping keys is present, its value wins over the env.
    """

    if config:
        for key in _CONFIG_KEYS:
            if key in config:
                return is_truthy_output_optimizer_flag(config.get(key))

    env = os.environ if environ is None else environ
    return is_truthy_output_optimizer_flag(env.get(OUTPUT_OPTIMIZER_ENV) if env is not None else None)


def output_optimizer_rtk_enabled(
    config: Mapping[str, Any] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Return whether the optional RTK adapter is explicitly enabled.

    RTK integration is disabled by default and remains independent from the
    native optimizer gate.  It can only participate when this dedicated config
    or env switch is true, so Egg never depends on RTK availability for native
    optimizer behavior.
    """

    if config:
        for key in _RTK_CONFIG_KEYS:
            if key in config:
                return is_truthy_output_optimizer_flag(config.get(key))

    env = os.environ if environ is None else environ
    return is_truthy_output_optimizer_flag(env.get(OUTPUT_OPTIMIZER_RTK_ENV) if env is not None else None)


def output_optimizer_rtk_command(
    config: Mapping[str, Any] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Return the RTK executable/command configured for the optional adapter."""

    if config:
        for key in _RTK_COMMAND_CONFIG_KEYS:
            value = config.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    env = os.environ if environ is None else environ
    value = env.get(OUTPUT_OPTIMIZER_RTK_BIN_ENV) if env is not None else None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return DEFAULT_OUTPUT_OPTIMIZER_RTK_COMMAND


def output_optimizer_rtk_timeout_seconds(
    config: Mapping[str, Any] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> float:
    """Return the bounded RTK adapter subprocess timeout in seconds."""

    values: list[Any] = []
    if config:
        values.extend(config.get(key) for key in _RTK_TIMEOUT_CONFIG_KEYS if key in config)
    env = os.environ if environ is None else environ
    if env is not None and OUTPUT_OPTIMIZER_RTK_TIMEOUT_ENV in env:
        values.append(env.get(OUTPUT_OPTIMIZER_RTK_TIMEOUT_ENV))

    for value in values:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return DEFAULT_OUTPUT_OPTIMIZER_RTK_TIMEOUT_SECONDS


def output_optimizer_rtk_privacy_opt_in(
    config: Mapping[str, Any] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Return True only when the user explicitly opts into normal RTK state/telemetry."""

    if config:
        for key in _RTK_PRIVACY_OPT_IN_CONFIG_KEYS:
            if key in config:
                return is_truthy_output_optimizer_flag(config.get(key))

    env = os.environ if environ is None else environ
    return is_truthy_output_optimizer_flag(
        env.get(OUTPUT_OPTIMIZER_RTK_PRIVACY_OPT_IN_ENV) if env is not None else None
    )


def normalize_output_optimizer_mode(value: Any) -> str:
    """Return a canonical output-optimizer mode or raise ``ValueError``."""

    text = str(value or "").strip().lower()
    if text not in OUTPUT_OPTIMIZER_MODES:
        allowed = "|".join(OUTPUT_OPTIMIZER_MODES)
        raise ValueError(f"Invalid output optimizer mode: {value!r}; expected one of {allowed}")
    return text


def output_optimizer_min_confidence_for_mode(mode: Any) -> float:
    """Return the policy min-confidence threshold for *mode*."""

    return float(OUTPUT_OPTIMIZER_MODE_MIN_CONFIDENCE[normalize_output_optimizer_mode(mode)])


@dataclass(frozen=True)
class OutputOptimizerThreadConfig:
    """Effective event-sourced output optimizer config for a thread.

    ``enabled=None`` means no explicit per-thread/ancestor enabled setting was
    found, so callers should continue using ``output_optimizer_enabled(...)``
    env/config fallback.  ``mode`` defaults to ``balanced``; policy code only
    receives it when an event exists, preserving no-event behavior.
    """

    enabled: bool | None = None
    mode: str = DEFAULT_OUTPUT_OPTIMIZER_MODE
    has_explicit_config: bool = False
    enabled_source: str = "env"
    mode_source: str = "default"
    source_thread_id: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", normalize_output_optimizer_mode(self.mode))
        object.__setattr__(self, "raw", MappingProxyType(dict(self.raw or {})))

    def effective_enabled(
        self,
        config: Mapping[str, Any] | None = None,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> bool:
        """Resolve enabled, falling back to the legacy env/config gate."""

        if self.enabled is not None:
            return bool(self.enabled)
        return output_optimizer_enabled(config, environ=environ)

    def to_policy_config(self) -> dict[str, Any]:
        """Return an ``OutputPolicyRequest.thread_config`` mapping.

        No-event configs intentionally return ``{}`` so the existing
        ``EGG_OUTPUT_OPTIMIZER`` / mapping behavior is exactly preserved.
        """

        if not self.has_explicit_config:
            return {}
        out: dict[str, Any] = {
            "output_optimizer_config_source": self.source,
        }
        if self.enabled is not None:
            out["output_optimizer_enabled"] = bool(self.enabled)
        out["output_optimizer_mode"] = self.mode
        out["output_optimizer_mode_min_confidence"] = output_optimizer_min_confidence_for_mode(self.mode)
        return out

    @property
    def source(self) -> str:
        if self.source_thread_id:
            return f"event:{self.source_thread_id}"
        return "event" if self.has_explicit_config else "env"


def _parent_id(db: "ThreadsDB", thread_id: str) -> Optional[str]:
    try:
        row = db.conn.execute(
            "SELECT parent_id FROM children WHERE child_id=? LIMIT 1",
            (thread_id,),
        ).fetchone()
        if row and isinstance(row[0], str) and row[0]:
            return row[0]
    except Exception:
        pass
    return None


def _ancestor_chain(db: "ThreadsDB", thread_id: str) -> list[str]:
    chain: list[str] = []
    tid: Optional[str] = thread_id
    seen: set[str] = set()
    while tid and tid not in seen:
        seen.add(tid)
        chain.append(tid)
        tid = _parent_id(db, tid)
    chain.reverse()
    return chain


def _payload_rows_for_thread(db: "ThreadsDB", thread_id: str) -> list[Mapping[str, Any]]:
    try:
        cur = db.conn.execute(
            "SELECT payload_json FROM events WHERE thread_id=? AND type=? ORDER BY event_seq ASC",
            (thread_id, OUTPUT_OPTIMIZER_CONFIG_EVENT_TYPE),
        )
    except Exception:
        return []

    payloads: list[Mapping[str, Any]] = []
    for (payload_json,) in cur.fetchall():
        try:
            payload = json.loads(payload_json) if isinstance(payload_json, str) else (payload_json or {})
        except Exception:
            payload = {}
        if isinstance(payload, Mapping):
            payloads.append(payload)
    return payloads


def _payload_enabled(payload: Mapping[str, Any]) -> bool | None:
    for key in _EVENT_ENABLED_KEYS:
        if key in payload:
            value = _coerce_optional_bool(payload.get(key))
            if value is not None:
                return value
    return None


def _payload_mode(payload: Mapping[str, Any]) -> str | None:
    for key in _EVENT_MODE_KEYS:
        if key in payload:
            try:
                return normalize_output_optimizer_mode(payload.get(key))
            except ValueError:
                return None
    return None


def get_thread_output_optimizer_config(db: "ThreadsDB", thread_id: str) -> OutputOptimizerThreadConfig:
    """Return effective inherited ``output_optimizer.config`` for a thread.

    Resolution is field-wise from ancestors to descendant, then event order on
    each thread.  Missing ``enabled`` preserves legacy env/config fallback;
    missing ``mode`` defaults to ``balanced``.
    """

    enabled: bool | None = None
    mode = DEFAULT_OUTPUT_OPTIMIZER_MODE
    has_explicit_config = False
    enabled_source = "env"
    mode_source = "default"
    source_thread_id: str | None = None
    raw: Mapping[str, Any] = {}

    try:
        chain = _ancestor_chain(db, thread_id)
    except Exception:
        chain = [thread_id]

    for tid in chain:
        for payload in _payload_rows_for_thread(db, tid):
            applied = False
            payload_enabled = _payload_enabled(payload)
            if payload_enabled is not None:
                enabled = payload_enabled
                enabled_source = f"event:{tid}"
                applied = True
            payload_mode = _payload_mode(payload)
            if payload_mode is not None:
                mode = payload_mode
                mode_source = f"event:{tid}"
                applied = True
            if applied:
                has_explicit_config = True
                source_thread_id = tid
                raw = dict(payload)

    return OutputOptimizerThreadConfig(
        enabled=enabled,
        mode=mode,
        has_explicit_config=has_explicit_config,
        enabled_source=enabled_source,
        mode_source=mode_source,
        source_thread_id=source_thread_id,
        raw=raw,
    )


def get_thread_output_optimizer_policy_config(db: "ThreadsDB", thread_id: str) -> dict[str, Any]:
    """Return the effective mapping to place on ``OutputPolicyRequest``."""

    return get_thread_output_optimizer_config(db, thread_id).to_policy_config()


def append_output_optimizer_config_event(
    db: "ThreadsDB",
    thread_id: str,
    *,
    enabled: bool | None = None,
    mode: str | None = None,
    reason: str = "user",
) -> None:
    """Append an incremental ``output_optimizer.config`` event."""

    payload: dict[str, Any] = {"reason": reason}
    if enabled is not None:
        payload["enabled"] = bool(enabled)
    if mode is not None:
        payload["mode"] = normalize_output_optimizer_mode(mode)
    if "enabled" not in payload and "mode" not in payload:
        raise ValueError("output optimizer config event requires enabled and/or mode")

    db.append_event(
        event_id=os.urandom(10).hex(),
        thread_id=thread_id,
        type_=OUTPUT_OPTIMIZER_CONFIG_EVENT_TYPE,
        msg_id=None,
        invoke_id=None,
        payload=payload,
    )


def set_thread_output_optimizer_enabled(db: "ThreadsDB", thread_id: str, enabled: bool, *, reason: str = "user") -> None:
    """Explicitly enable or disable the native optimizer for a thread."""

    append_output_optimizer_config_event(db, thread_id, enabled=bool(enabled), reason=reason)


def set_thread_output_optimizer_mode(db: "ThreadsDB", thread_id: str, mode: str, *, reason: str = "user") -> str:
    """Set the optimizer mode for a thread and return its canonical value."""

    normalized = normalize_output_optimizer_mode(mode)
    append_output_optimizer_config_event(db, thread_id, mode=normalized, reason=reason)
    return normalized


def format_thread_output_optimizer_status(
    db: "ThreadsDB",
    thread_id: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Return a compact user-facing status string for slash commands/UIs."""

    cfg = get_thread_output_optimizer_config(db, thread_id)
    enabled = cfg.effective_enabled(environ=environ)
    status = "ENABLED" if enabled else "DISABLED"
    if cfg.enabled is None:
        env = os.environ if environ is None else environ
        env_value = env.get(OUTPUT_OPTIMIZER_ENV) if env is not None else None
        enabled_source = f"env:{OUTPUT_OPTIMIZER_ENV}={env_value!r}" if env_value is not None else "default disabled"
    else:
        enabled_source = cfg.enabled_source
    min_confidence = output_optimizer_min_confidence_for_mode(cfg.mode)
    lines = [
        "Native output optimizer:",
        f"  Enabled: {status}",
        f"  Enabled source: {enabled_source}",
        f"  Mode: {cfg.mode}",
        f"  Mode source: {cfg.mode_source}",
        f"  Min confidence: {min_confidence:g}",
        f"  Event config present: {bool(cfg.has_explicit_config)}",
    ]
    return "\n".join(lines)


__all__ = [
    "DEFAULT_OUTPUT_OPTIMIZER_MODE",
    "DEFAULT_OUTPUT_OPTIMIZER_RTK_COMMAND",
    "DEFAULT_OUTPUT_OPTIMIZER_RTK_TIMEOUT_SECONDS",
    "OUTPUT_OPTIMIZER_CONFIG_EVENT_TYPE",
    "OUTPUT_OPTIMIZER_ENV",
    "OUTPUT_OPTIMIZER_MODE_MIN_CONFIDENCE",
    "OUTPUT_OPTIMIZER_MODES",
    "OUTPUT_OPTIMIZER_RTK_BIN_ENV",
    "OUTPUT_OPTIMIZER_RTK_ENV",
    "OUTPUT_OPTIMIZER_RTK_PRIVACY_OPT_IN_ENV",
    "OUTPUT_OPTIMIZER_RTK_TIMEOUT_ENV",
    "OutputOptimizerThreadConfig",
    "append_output_optimizer_config_event",
    "format_thread_output_optimizer_status",
    "get_thread_output_optimizer_config",
    "get_thread_output_optimizer_policy_config",
    "is_truthy_output_optimizer_flag",
    "normalize_output_optimizer_mode",
    "output_optimizer_enabled",
    "output_optimizer_min_confidence_for_mode",
    "output_optimizer_rtk_command",
    "output_optimizer_rtk_enabled",
    "output_optimizer_rtk_privacy_opt_in",
    "output_optimizer_rtk_timeout_seconds",
    "set_thread_output_optimizer_enabled",
    "set_thread_output_optimizer_mode",
]
