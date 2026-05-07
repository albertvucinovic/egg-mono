from __future__ import annotations

"""Built-in web search/fetch tools."""

import os
from dataclasses import dataclass
from typing import Any, Dict

from ..plugins import PluginContext
from ..tools import ToolRegistry
from ..web import WebBackendError, get_backend

WEB_RESULTS_CAP = 25


def resolve_max_results(args: Dict[str, Any]) -> int:
    raw = args.get("max_results")
    if raw is None:
        raw = os.environ.get("EGG_WEB_MAX_RESULTS")
    try:
        n = int(raw) if raw is not None and str(raw).strip() != "" else 10
    except (TypeError, ValueError):
        n = 10
    if n < 1:
        n = 1
    if n > WEB_RESULTS_CAP:
        n = WEB_RESULTS_CAP
    return n


def web_search_tool(args: Dict[str, Any]) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        return 'Error: "query" is required.'
    n = resolve_max_results(args)
    try:
        backend = get_backend()
        results = backend.search(query, max_results=n)
    except WebBackendError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error: web_search failed: {e}"
    if not results:
        return "No results."
    lines = []
    for r in results:
        if not (r.title or r.url):
            continue
        snippet = (r.snippet or "").strip().replace("\n", " ")
        if len(snippet) > 200:
            snippet = snippet[:200].rstrip() + "…"
        if snippet:
            lines.append(f"- {r.title}  {r.url}\n    {snippet}")
        else:
            lines.append(f"- {r.title}  {r.url}")
    return "\n".join(lines)


def fetch_url_tool(args: Dict[str, Any]) -> str:
    url = str(args.get("url") or "").strip()
    if not url:
        return 'Error: "url" is required.'
    try:
        backend = get_backend()
        return backend.fetch(url)
    except WebBackendError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error: fetch_url failed: {e}"


def register_web_tools(registry: ToolRegistry) -> None:
    search_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query."},
            "max_results": {
                "type": "integer",
                "description": f"Maximum number of results to return (default 10, max {WEB_RESULTS_CAP}).",
                "minimum": 1,
                "maximum": WEB_RESULTS_CAP,
            },
        },
        "required": ["query"],
    }
    fetch_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to fetch."},
        },
        "required": ["url"],
    }

    registry.register(
        name="web_search",
        description=(
            "Perform a web search and return results with titles, URLs, and short snippets. "
            f"Defaults to 10 results (cap {WEB_RESULTS_CAP}); pass max_results to adjust. "
            "Backend is selected via EGG_WEB_BACKEND (default: searxng)."
        ),
        parameters_schema=search_schema,
        impl=web_search_tool,
    )
    registry.register(
        name="fetch_url",
        description=(
            "Fetch and extract readable markdown from a URL. Use this when you "
            "already know the page URL."
        ),
        parameters_schema=fetch_schema,
        impl=fetch_url_tool,
    )


@dataclass(frozen=True)
class WebPlugin:
    name: str = "web"
    version: str = "0"

    def register(self, context: PluginContext) -> None:
        register_web_tools(context.tool_registry)


__all__ = [
    "WEB_RESULTS_CAP",
    "WebPlugin",
    "fetch_url_tool",
    "register_web_tools",
    "resolve_max_results",
    "web_search_tool",
]
