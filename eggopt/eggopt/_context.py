from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any
from weakref import WeakValueDictionary

_CURRENT_EVALUATION: ContextVar[Mapping[str, Any] | None] = ContextVar(
    "eggopt_native_evaluation", default=None
)
_RUNTIME_BY_KEY: WeakValueDictionary[str, Any] = WeakValueDictionary()


def current_evaluation() -> Mapping[str, Any]:
    """Return the current case's public thread and workspace references."""

    context = _current_evaluation()
    return {name: value for name, value in context.items() if not name.startswith("_")}


def _current_evaluation() -> Mapping[str, Any]:
    context = _CURRENT_EVALUATION.get()
    if context is None:
        raise RuntimeError("current_evaluation() is only available inside an evaluator")
    return context


@contextmanager
def _evaluation_scope(context: Mapping[str, Any]) -> Iterator[None]:
    token = _CURRENT_EVALUATION.set(context)
    try:
        yield
    finally:
        _CURRENT_EVALUATION.reset(token)


def _bind_evaluation_runtime(key: str, runtime: Any) -> None:
    _RUNTIME_BY_KEY[key] = runtime


def _evaluation_runtime(key: str) -> Any:
    try:
        return _RUNTIME_BY_KEY[key]
    except KeyError as exc:
        raise RuntimeError("evaluation runtime is not open") from exc
