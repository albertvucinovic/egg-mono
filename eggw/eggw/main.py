"""FastAPI backend for eggw - Web UI for eggthreads."""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.middleware.cors import CORSMiddleware

from eggthreads import ThreadsDB

# Mock LLM for testing
from .mock_llm import is_test_mode, get_llm_client

# Import core state management
from . import core
from .core import state as core_state
from .core import load_models_config
from .security import ApiAuthorizationMiddleware, SecurityConfig


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    # Change to the caller's working directory if specified
    # This ensures sandbox configs, models.json, etc. are found correctly
    egg_cwd = os.environ.get("EGG_CWD")
    resolved_egg_cwd = Path(egg_cwd).expanduser().resolve() if egg_cwd else None
    if egg_cwd:
        os.chdir(resolved_egg_cwd)
        print(f"Working directory: {resolved_egg_cwd}")

    core_state.configure_model_paths(resolved_egg_cwd)

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
    core_state.image_generation_models_config, core_state.default_image_generation_model_key = (
        core.load_image_generation_models_config()
    )

    # Initialize LLM client using the same resolved model paths as the routes,
    # scheduler, and commands.
    models_path = core_state.MODELS_PATH
    all_models_path = core_state.ALL_MODELS_PATH
    try:
        from eggllm import LLMClient
        core_state.llm_client = LLMClient(models_path=models_path, all_models_path=all_models_path)
        print(f"Models config: {models_path}")
        print(f"All-models catalog: {all_models_path}")
        print(f"Image generation models config: {core_state.IMAGE_GENERATION_MODELS_PATH}")
    except Exception as e:
        print(f"Warning: Could not initialize LLM client: {e}")
        core_state.llm_client = None

    # Note: SubtreeScheduler requires a root_thread_id to watch.
    # For per-thread scheduling, we'll start schedulers on-demand when messages are sent.
    # Global scheduler initialization is skipped for now.

    yield

    # Cleanup
    pass


security_config = SecurityConfig.from_env()


app = FastAPI(
    title="eggw API",
    description="Web API for eggthreads",
    version="0.1.0",
    lifespan=lifespan,
)

# Authentication is transport-wide rather than copied onto individual routes.
# Add CORS last so it is the outer middleware and authenticated/denied responses
# receive the appropriate headers for an allowed browser origin.
app.add_middleware(ApiAuthorizationMiddleware, config=security_config)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(security_config.allowed_origins),
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Last-Event-ID"],
)

# Register routers
from .routes import (
    threads_router,
    messages_router,
    tools_router,
    models_router,
    settings_router,
    stats_router,
    events_router,
    commands_router,
    edit_answer_router,
    health_router,
    auth_router,
)
from .autocomplete import autocomplete_router

app.include_router(threads_router)
app.include_router(messages_router)
app.include_router(tools_router)
app.include_router(models_router)
app.include_router(settings_router)
app.include_router(stats_router)
app.include_router(events_router)
app.include_router(commands_router)
app.include_router(edit_answer_router)
app.include_router(health_router)
app.include_router(auth_router)
app.include_router(autocomplete_router)


@app.get("/")
async def root_redirect():
    """Redirect to the reloaded thread when eggw.sh restarts after /reload."""
    thread_id = (os.environ.get("EGGW_RELOAD_THREAD_ID") or "").strip()
    if thread_id:
        return RedirectResponse(url=f"/{thread_id}")
    return {"status": "ok", "app": "eggw"}


if __name__ == "__main__":
    from hypercorn.config import Config
    from hypercorn.asyncio import serve

    config = Config()
    config.bind = ["127.0.0.1:8000"]
    asyncio.run(serve(app, config))
