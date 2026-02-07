"""FastAPI backend for eggw - Web UI for eggthreads."""
from __future__ import annotations

import asyncio
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Mock LLM for testing
from mock_llm import is_test_mode, get_llm_client

# Add parent directories to path for eggthreads/eggllm imports
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "eggthreads"))
sys.path.insert(0, str(PROJECT_ROOT / "eggllm"))

from eggthreads import ThreadsDB

# Import core state management
import core
from core import state as core_state
from core import load_models_config, MODELS_PATH


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    # Change to the caller's working directory if specified
    # This ensures sandbox configs, models.json, etc. are found correctly
    egg_cwd = os.environ.get("EGG_CWD")
    if egg_cwd:
        os.chdir(egg_cwd)
        print(f"Working directory: {egg_cwd}")

    # Initialize database - use EGG_DB_PATH if specified, else default
    db_path = os.environ.get("EGG_DB_PATH")
    if db_path:
        core_state.db = ThreadsDB(db_path)
    else:
        core_state.db = ThreadsDB()  # Uses default .egg/threads.sqlite
    # Log the absolute path being used
    print(f"Database: {core_state.db.path.absolute()}")
    core_state.db.init_schema()  # Create tables if they don't exist

    # Load models
    core_state.models_config, core_state.default_model_key = load_models_config()

    # Initialize LLM client
    # Look for models.json in CWD first, then fall back to egg directory
    cwd_models = Path.cwd() / "models.json"
    egg_models = PROJECT_ROOT / "eggconfig" / "models.json"
    models_path = cwd_models if cwd_models.exists() else egg_models
    try:
        from eggllm import LLMClient
        core_state.llm_client = LLMClient(models_path=models_path)
        print(f"Models config: {models_path}")
    except Exception as e:
        print(f"Warning: Could not initialize LLM client: {e}")
        core_state.llm_client = None

    # Note: SubtreeScheduler requires a root_thread_id to watch.
    # For per-thread scheduling, we'll start schedulers on-demand when messages are sent.
    # Global scheduler initialization is skipped for now.

    yield

    # Cleanup
    pass


app = FastAPI(
    title="eggw API",
    description="Web API for eggthreads",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS middleware for frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow all origins for development
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
from routes import (
    threads_router,
    messages_router,
    tools_router,
    models_router,
    settings_router,
    stats_router,
    events_router,
    commands_router,
    health_router,
    auth_router,
)
from autocomplete import autocomplete_router

app.include_router(threads_router)
app.include_router(messages_router)
app.include_router(tools_router)
app.include_router(models_router)
app.include_router(settings_router)
app.include_router(stats_router)
app.include_router(events_router)
app.include_router(commands_router)
app.include_router(health_router)
app.include_router(auth_router)
app.include_router(autocomplete_router)


if __name__ == "__main__":
    from hypercorn.config import Config
    from hypercorn.asyncio import serve

    config = Config()
    config.bind = ["0.0.0.0:8000"]
    asyncio.run(serve(app, config))
