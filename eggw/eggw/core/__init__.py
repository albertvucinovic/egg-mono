"""Core modules for eggw backend."""
from . import state
from .state import (
    DB_PATH,
    configure_model_paths,
    init_db,
    get_db,
    resolve_image_generation_models_path,
    resolve_model_paths,
)
from .config import (
    chat_model_keys,
    effective_model_config,
    is_chat_model_key,
    load_image_generation_models_config,
    load_models_config,
    shorten_output_preview,
)
from .scheduler import (
    get_thread_root_id,
    start_scheduler,
    stop_scheduler,
    ensure_scheduler_for,
    scheduler_running,
)


# Dynamic attribute access for mutable state variables
# This ensures core.db always returns the current value from state.db
_DYNAMIC_ATTRS = {
    'db',
    'llm_client',
    'models_config',
    'default_model_key',
    'image_generation_models_config',
    'default_image_generation_model_key',
    'active_schedulers',
    'MODELS_PATH',
    'ALL_MODELS_PATH',
    'IMAGE_GENERATION_MODELS_PATH',
}


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
    "image_generation_models_config",
    "default_image_generation_model_key",
    "active_schedulers",
    "MODELS_PATH",
    "ALL_MODELS_PATH",
    "IMAGE_GENERATION_MODELS_PATH",
    "DB_PATH",
    "configure_model_paths",
    "resolve_model_paths",
    "resolve_image_generation_models_path",
    "init_db",
    "get_db",
    # Config
    "load_models_config",
    "load_image_generation_models_config",
    "shorten_output_preview",
    "effective_model_config",
    "is_chat_model_key",
    "chat_model_keys",
    # Scheduler
    "get_thread_root_id",
    "start_scheduler",
    "stop_scheduler",
    "ensure_scheduler_for",
    "scheduler_running",
]
