from __future__ import annotations

"""Container-side eggtools module for Docker REPL sessions.

This module communicates with the host bridge through a small file-based RPC
protocol over the mounted bridge directory.  It intentionally does not access
Egg's SQLite database directly.
"""

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional


def _bridge_dir() -> Path:
    raw = os.environ.get("EGG_BRIDGE_DIR") or "/egg-bridge"
    p = Path(raw)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _eval_token() -> str:
    token = os.environ.get("EGG_EVAL_TOKEN") or ""
    if not token:
        raise RuntimeError("EGG_EVAL_TOKEN is not set; eggtools is only available during an Egg REPL eval")
    return token


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, path)


def tool(name: str, timeout_sec: Optional[float] = None, **kwargs: Any) -> str:
    """Call an Egg tool through the host bridge and return its string result."""

    bridge = _bridge_dir()
    req_id = uuid.uuid4().hex
    req_path = bridge / f"tool_{req_id}.req.json"
    res_path = bridge / f"tool_{req_id}.res.json"
    if timeout_sec is None:
        try:
            timeout_sec = float(os.environ.get("EGG_TOOL_TIMEOUT", "30"))
        except Exception:
            timeout_sec = 30.0
    _atomic_write_json(req_path, {
        "id": req_id,
        "token": _eval_token(),
        "name": name,
        "arguments": kwargs,
        "timeout_sec": timeout_sec,
    })
    start = time.time()
    while True:
        if res_path.exists():
            try:
                payload = json.loads(res_path.read_text(encoding="utf-8"))
            finally:
                try:
                    res_path.unlink()
                except Exception:
                    pass
            if payload.get("ok"):
                return str(payload.get("result") or "")
            raise RuntimeError(str(payload.get("error") or "Egg tool call failed"))
        if timeout_sec is not None and (time.time() - start) >= float(timeout_sec):
            raise TimeoutError(f"Egg tool call timed out: {name}")
        time.sleep(0.05)


def spawn_agent(context_text: str, **kwargs: Any) -> str:
    kwargs["context_text"] = context_text
    return tool("spawn_agent", **kwargs)


def spawn_agent_auto(context_text: str, **kwargs: Any) -> str:
    kwargs["context_text"] = context_text
    return tool("spawn_agent_auto", **kwargs)


def wait(thread_ids: list[str], **kwargs: Any) -> str:
    kwargs["thread_ids"] = thread_ids
    return tool("wait", **kwargs)


def web_search(query: str, **kwargs: Any) -> str:
    kwargs["query"] = query
    return tool("web_search", **kwargs)


def fetch_url(url: str, **kwargs: Any) -> str:
    kwargs["url"] = url
    return tool("fetch_url", **kwargs)


def bash(script: str, **kwargs: Any) -> str:
    kwargs["script"] = script
    return tool("bash", **kwargs)


def python(script: str, **kwargs: Any) -> str:
    kwargs["script"] = script
    return tool("python", **kwargs)
