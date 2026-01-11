"""API routes for eggw backend."""
from .threads import router as threads_router
from .messages import router as messages_router
from .tools import router as tools_router
from .models import router as models_router
from .settings import router as settings_router
from .stats import router as stats_router
from .events import router as events_router
from .commands import router as commands_router
from .health import router as health_router

__all__ = [
    "threads_router",
    "messages_router",
    "tools_router",
    "models_router",
    "settings_router",
    "stats_router",
    "events_router",
    "commands_router",
    "health_router",
]
