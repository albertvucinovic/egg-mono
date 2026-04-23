from __future__ import annotations

from .base import SearchResult, WebBackend, WebBackendError
from .factory import get_backend

__all__ = ["SearchResult", "WebBackend", "WebBackendError", "get_backend"]
