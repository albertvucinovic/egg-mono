from __future__ import annotations

"""Built-in web search/fetch tools and SearXNG commands."""

import os
import shutil
import subprocess
import threading
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ..plugins import PluginContext
from ..tools import ToolRegistry
from ..web import SearchResponse, WebBackendError, get_fetch_orchestrator, get_search_orchestrator

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
        orchestrator = get_search_orchestrator()
        response = orchestrator.search_response(query, max_results=n)
    except WebBackendError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error: web_search failed: {e}"
    results = response.results
    if not results:
        return _format_empty_search_response(response)
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


def _format_empty_search_response(response: SearchResponse) -> str:
    if response.degraded_empty:
        lines = ["Search backend degraded; no reliable results returned."]
        for message in response.diagnostic_messages()[:3]:
            lines.append(f"- {message}")
        return "\n".join(lines)
    return "No matching results found."


def fetch_url_tool(args: Dict[str, Any]) -> str:
    url = str(args.get("url") or "").strip()
    if not url:
        return 'Error: "url" is required.'
    try:
        orchestrator = get_fetch_orchestrator()
        return orchestrator.fetch(url)
    except WebBackendError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error: fetch_url failed: {e}"


def find_searxng_dir() -> Optional[Path]:
    """Locate the packaged SearXNG docker-compose directory."""

    try:
        import eggthreads.web.searxng as _pkg

        pkg_dir = Path(_pkg.__file__).resolve().parent
    except Exception:
        pkg_dir = None

    if pkg_dir is not None and (pkg_dir / "docker-compose.yml").is_file():
        return pkg_dir

    candidates: List[Path] = []
    here = Path(__file__).resolve().parent
    for _ in range(6):
        candidates.append(here / "searxng")
        candidates.append(here / "eggthreads" / "eggthreads" / "web" / "searxng")
        if here.parent == here:
            break
        here = here.parent
    seen: set[Path] = set()
    for candidate in candidates:
        candidate = candidate.resolve()
        if candidate in seen:
            continue
        seen.add(candidate)
        if (candidate / "docker-compose.yml").is_file():
            return candidate
    return None


def resolve_compose_cmd() -> Optional[List[str]]:
    """Return the argv prefix for docker compose, preferring v2 plugin."""

    if shutil.which("docker"):
        try:
            probe = subprocess.run(
                ["docker", "compose", "version"],
                capture_output=True,
                timeout=5,
            )
            if probe.returncode == 0:
                return ["docker", "compose"]
        except Exception:
            pass
    if shutil.which("docker-compose"):
        return ["docker-compose"]
    return None


def _log(context: Any, message: str) -> None:
    if context.log_system is not None:
        context.log_system(message)


def _print_block(context: Any, title: str, text: str, *, border_style: str) -> None:
    if context.console_print_block is not None:
        context.console_print_block(title, text, border_style=border_style)
    else:
        _log(context, text)


def run_searxng_compose(
    context: Any,
    compose_args: List[str],
    *,
    action: str,
    starting_msg: str,
    success_summary: str,
    timeout_sec: int = 600,
) -> None:
    """Run docker compose in the packaged SearXNG directory on a thread."""

    searxng_dir = find_searxng_dir()
    if searxng_dir is None:
        msg = (
            "could not locate searxng/docker-compose.yml "
            "(expected a 'searxng/' dir near the egg-mono root)."
        )
        _log(context, f"SearXNG {action}: {msg}")
        _print_block(context, f"SearXNG {action}", msg, border_style="red")
        return

    compose = resolve_compose_cmd()
    if compose is None:
        msg = (
            "neither 'docker compose' nor 'docker-compose' is on PATH. "
            "Install Docker Engine + compose, then re-run the command."
        )
        _log(context, f"SearXNG {action}: {msg}")
        _print_block(context, f"SearXNG {action}", msg, border_style="red")
        return

    argv = compose + compose_args
    _log(
        context,
        f"SearXNG {action}: {starting_msg} (running `{' '.join(argv)}` "
        f"in {searxng_dir}; see console for output)",
    )

    def _runner() -> None:
        try:
            proc = subprocess.run(
                argv,
                cwd=str(searxng_dir),
                capture_output=True,
                text=True,
                timeout=timeout_sec,
            )
        except subprocess.TimeoutExpired:
            try:
                _log(context, f"SearXNG {action}: timed out after {timeout_sec}s.")
                _print_block(
                    context,
                    f"SearXNG {action}",
                    f"Timed out after {timeout_sec}s running `{' '.join(argv)}`.",
                    border_style="red",
                )
            except Exception:
                pass
            return
        except Exception as e:
            try:
                _log(context, f"SearXNG {action}: failed to launch: {e}")
                _print_block(
                    context,
                    f"SearXNG {action}",
                    f"Failed to launch `{' '.join(argv)}`:\n{e}",
                    border_style="red",
                )
            except Exception:
                pass
            return

        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
        combined_parts: List[str] = []
        if stdout:
            combined_parts.append(stdout)
        if stderr:
            combined_parts.append(stderr)
        combined = "\n".join(combined_parts) or "(no output)"
        if len(combined) > 4000:
            combined = combined[:4000] + "\n... (truncated)"

        if proc.returncode == 0:
            try:
                _log(context, f"SearXNG {action}: done. {success_summary}")
                _print_block(
                    context,
                    f"SearXNG {action}",
                    f"{success_summary}\n\n$ {' '.join(argv)}\n{combined}",
                    border_style="green",
                )
            except Exception:
                pass
        else:
            try:
                _log(context, f"SearXNG {action}: failed (exit {proc.returncode}). See console.")
                _print_block(
                    context,
                    f"SearXNG {action}",
                    f"Exit code: {proc.returncode}\n$ {' '.join(argv)}\n{combined}",
                    border_style="red",
                )
            except Exception:
                pass

    threading.Thread(target=_runner, name=f"searxng-{action}", daemon=True).start()


def start_searxng_command(context: Any, arg: str):
    from ..command_catalog import CommandResult

    run_searxng_compose(
        context,
        ["up", "-d"],
        action="start",
        starting_msg="starting container (first run may pull the image)",
        success_summary=(
            "Container up at http://localhost:8888. "
            "web_search / fetch_url will now use SearXNG."
        ),
        timeout_sec=600,
    )
    return CommandResult(clear_input=True)


def stop_searxng_command(context: Any, arg: str):
    from ..command_catalog import CommandResult

    run_searxng_compose(
        context,
        ["down"],
        action="stop",
        starting_msg="stopping container",
        success_summary=(
            "Container stopped. web_search / fetch_url will now fail "
            "until you /startSearxng again or switch backends "
            "(EGG_WEB_BACKEND=tavily)."
        ),
        timeout_sec=120,
    )
    return CommandResult(clear_input=True)


def register_web_commands(registry: Any) -> None:
    from ..command_catalog import CommandSpec

    registry.register(CommandSpec("startSearxng", start_searxng_command, category="web", usage="/startSearxng", description="Start the local SearXNG backend."))
    registry.register(CommandSpec("stopSearxng", stop_searxng_command, category="web", usage="/stopSearxng", description="Stop the local SearXNG backend."))


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
            "Backend is selected via EGG_WEB_BACKEND (default: auto)."
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
        if context.tool_registry is not None:
            register_web_tools(context.tool_registry)
        if context.command_registry is not None:
            register_web_commands(context.command_registry)


__all__ = [
    "WEB_RESULTS_CAP",
    "WebPlugin",
    "fetch_url_tool",
    "find_searxng_dir",
    "register_web_commands",
    "register_web_tools",
    "resolve_compose_cmd",
    "resolve_max_results",
    "run_searxng_compose",
    "start_searxng_command",
    "stop_searxng_command",
    "web_search_tool",
]
