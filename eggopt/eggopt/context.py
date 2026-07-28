from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any
from weakref import WeakValueDictionary

_CURRENT_OPERATION: ContextVar[Mapping[str, Any] | None] = ContextVar(
    "eggopt_operation", default=None
)
_RUNTIME_BY_KEY: WeakValueDictionary[str, Any] = WeakValueDictionary()


def current_evaluation() -> Mapping[str, Any]:
    """Return the current case's public thread and workspace references."""

    context = current_operation()
    return {name: value for name, value in context.items() if not name.startswith("_")}


def current_operation() -> Mapping[str, Any]:
    """Return the current Eggopt operation's public context."""

    context = _current_operation()
    return {name: value for name, value in context.items() if not name.startswith("_")}


def _current_evaluation_context_limit() -> int | None:
    value = _current_evaluation().get("_context_limit")
    return int(value) if value is not None else None


def _current_evaluation() -> Mapping[str, Any]:
    return _current_operation()


def _current_operation() -> Mapping[str, Any]:
    context = _CURRENT_OPERATION.get()
    if context is None:
        raise RuntimeError("current_operation() is only available inside an operation")
    return context


@contextmanager
def _evaluation_scope(context: Mapping[str, Any]) -> Iterator[None]:
    with _operation_scope(context):
        yield


@contextmanager
def _operation_scope(context: Mapping[str, Any]) -> Iterator[None]:
    token = _CURRENT_OPERATION.set(context)
    try:
        yield
    finally:
        _CURRENT_OPERATION.reset(token)


def _bind_evaluation_runtime(key: str, runtime: Any) -> None:
    _bind_operation_runtime(key, runtime)


def _bind_operation_runtime(key: str, runtime: Any) -> None:
    _RUNTIME_BY_KEY[key] = runtime


def _evaluation_runtime(key: str) -> Any:
    return _operation_runtime(key)


def _operation_runtime(key: str) -> Any:
    try:
        return _RUNTIME_BY_KEY[key]
    except KeyError as exc:
        raise RuntimeError("operation runtime is not open") from exc
