"""Global state management for eggw backend."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional

from eggconfig import get_all_models_path, get_image_generation_models_path, get_models_path
from eggthreads import ThreadsDB, SubtreeScheduler

# Paths


def _base_cwd(cwd: Path | str | None = None) -> Path:
    """Return the working directory EggW should use for project-local files."""

    raw = cwd if cwd is not None else os.environ.get("EGG_CWD")
    return Path(raw).expanduser().resolve() if raw else Path.cwd().resolve()


def _env_path(name: str, base: Path) -> Optional[Path]:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def resolve_model_paths(cwd: Path | str | None = None) -> tuple[Path, Path]:
    """Resolve the models/all-models files used by the EggW backend.

    EggW is normally launched from the monorepo but points at a caller working
    directory through ``EGG_CWD``.  Prefer that directory's ``models.json`` when
    present so the web header, model selector, scheduler, and commands all use
    the same model configuration as the active thread database.  Explicit env
    overrides win over both local and packaged defaults.
    """

    base = _base_cwd(cwd)
    packaged_models = get_models_path().resolve()
    packaged_all_models = get_all_models_path().resolve()

    models_path = _env_path("EGG_MODELS_PATH", base)
    if models_path is None:
        local_models = base / "models.json"
        models_path = local_models.resolve() if local_models.exists() else packaged_models

    all_models_path = _env_path("EGG_ALL_MODELS_PATH", base)
    if all_models_path is None:
        if models_path == packaged_models:
            all_models_path = packaged_all_models
        else:
            # Keep catalog updates/discovery next to the selected local models
            # file, even when all-models.json does not exist yet.
            all_models_path = models_path.with_name("all-models.json").resolve()

    return models_path, all_models_path


def resolve_image_generation_models_path(cwd: Path | str | None = None) -> Path:
    """Resolve the image-generation-models.json file used by EggW."""

    base = _base_cwd(cwd)
    packaged_image_models = get_image_generation_models_path().resolve()
    image_models_path = _env_path("EGG_IMAGE_GENERATION_MODELS_PATH", base)
    if image_models_path is not None:
        return image_models_path
    local_image_models = base / "image-generation-models.json"
    if local_image_models.exists():
        return local_image_models.resolve()
    return packaged_image_models


def configure_model_paths(cwd: Path | str | None = None) -> tuple[Path, Path]:
    """Refresh global model path state and return ``(models, all_models)``."""

    global MODELS_PATH, ALL_MODELS_PATH, IMAGE_GENERATION_MODELS_PATH
    MODELS_PATH, ALL_MODELS_PATH = resolve_model_paths(cwd)
    IMAGE_GENERATION_MODELS_PATH = resolve_image_generation_models_path(cwd)
    return MODELS_PATH, ALL_MODELS_PATH


MODELS_PATH, ALL_MODELS_PATH = resolve_model_paths()
IMAGE_GENERATION_MODELS_PATH = resolve_image_generation_models_path()
DB_PATH = Path(".egg/threads.sqlite")

# Global state - initialized in lifespan
db: Optional[ThreadsDB] = None
llm_client = None
models_config: Dict[str, Any] = {}
default_model_key: Optional[str] = None
image_generation_models_config: Dict[str, Any] = {}
default_image_generation_model_key: Optional[str] = None

# Active schedulers: root_thread_id -> {"scheduler": SubtreeScheduler, "task": Task}
active_schedulers: Dict[str, Dict[str, Any]] = {}


def init_db(path: Path = DB_PATH) -> ThreadsDB:
    """Initialize the database connection."""
    global db
    path.parent.mkdir(parents=True, exist_ok=True)
    db = ThreadsDB(str(path))
    return db


def get_db() -> ThreadsDB:
    """Get the database connection, raising if not initialized."""
    if db is None:
        raise RuntimeError("Database not initialized")
    return db
