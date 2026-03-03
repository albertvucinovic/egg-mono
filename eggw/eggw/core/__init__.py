"""Core modules for eggw backend."""
from . import state
from .state import (
    MODELS_PATH,
    ALL_MODELS_PATH,
    DB_PATH,
    PROJECT_ROOT,
    init_db,
    get_db,
)
from .config import load_models_config, shorten_output_preview
from .scheduler import (
    get_thread_root_id,
    start_scheduler,
    stop_scheduler,
    ensure_scheduler_for,
)


# Dynamic attribute access for mutable state variables
# This ensures core.db always returns the current value from state.db
_DYNAMIC_ATTRS = {'db', 'llm_client', 'models_config', 'default_model_key', 'active_schedulers'}


def __getattr__(name):
    if name in _DYNAMIC_ATTRS:
        return getattr(state, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    # State
    "db",
    "llm_client",
    "models_config",
    "default_model_key",
    "active_schedulers",
    "MODELS_PATH",
    "ALL_MODELS_PATH",
    "DB_PATH",
    "PROJECT_ROOT",
    "init_db",
    "get_db",
    # Config
    "load_models_config",
    "shorten_output_preview",
    # Scheduler
    "get_thread_root_id",
    "start_scheduler",
    "stop_scheduler",
    "ensure_scheduler_for",
]
