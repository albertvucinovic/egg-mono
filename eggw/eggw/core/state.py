"""Global state management for eggw backend."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from eggconfig import get_models_path, get_all_models_path
from eggthreads import ThreadsDB, SubtreeScheduler

# Paths
MODELS_PATH = get_models_path()
ALL_MODELS_PATH = get_all_models_path()
DB_PATH = Path(".egg/threads.sqlite")

# Global state - initialized in lifespan
db: Optional[ThreadsDB] = None
llm_client = None
models_config: Dict[str, Any] = {}
default_model_key: Optional[str] = None

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
