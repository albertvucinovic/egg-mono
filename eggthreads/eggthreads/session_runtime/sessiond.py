from __future__ import annotations

"""Container-side session daemon for explicit RLM Docker sessions."""

import argparse
import ast
import contextlib
import io
import json
import os
import sys
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Dict


PY_REPLS: Dict[str, Dict[str, Any]] = {}


def atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, path)


def format_output(stdout_text: str, stderr_text: str) -> str:
    out = ""
    stdout_text = (stdout_text or "").strip()
    stderr_text = (stderr_text or "").strip()
    if stdout_text:
        out += f"--- STDOUT ---\n{stdout_text}\n"
    if stderr_text:
        out += f"--- STDERR ---\n{stderr_text}\n"
    return out.strip() or "--- The Python REPL executed successfully and produced no output ---"


def execute_python(code: str, repl_name: str, bridge_dir: Path, token: str, runtime_dir: Path) -> str:
    globs = PY_REPLS.setdefault(repl_name or "default", {"__name__": "__egg_repl__"})
    sys.path.insert(0, str(runtime_dir)) if str(runtime_dir) not in sys.path else None
    old_bridge = os.environ.get("EGG_BRIDGE_DIR")
    old_token = os.environ.get("EGG_EVAL_TOKEN")
    os.environ["EGG_BRIDGE_DIR"] = str(bridge_dir)
    os.environ["EGG_EVAL_TOKEN"] = token
    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        tree = ast.parse(code or "", mode="exec")
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            if tree.body and isinstance(tree.body[-1], ast.Expr):
                body = tree.body[:-1]
                expr = tree.body[-1].value
                if body:
                    exec(compile(ast.Module(body=body, type_ignores=[]), "<egg-docker-python-repl>", "exec"), globs, globs)
                value = eval(compile(ast.Expression(expr), "<egg-docker-python-repl>", "eval"), globs, globs)
                if value is not None:
                    print(repr(value))
            else:
                exec(compile(tree, "<egg-docker-python-repl>", "exec"), globs, globs)
    except Exception:
        traceback.print_exc(file=stderr)
    finally:
        if old_bridge is None:
            os.environ.pop("EGG_BRIDGE_DIR", None)
        else:
            os.environ["EGG_BRIDGE_DIR"] = old_bridge
        if old_token is None:
            os.environ.pop("EGG_EVAL_TOKEN", None)
        else:
            os.environ["EGG_EVAL_TOKEN"] = old_token
    return format_output(stdout.getvalue(), stderr.getvalue())


def claim(path: Path) -> Path | None:
    claimed = path.with_suffix(path.suffix + ".processing")
    try:
        os.replace(path, claimed)
        return claimed
    except FileNotFoundError:
        return None
    except Exception:
        return None


def process_eval_request(req_path: Path, bridge_dir: Path, runtime_dir: Path) -> None:
    claimed = claim(req_path)
    if claimed is None:
        return
    req_id = req_path.name[len("eval_"):-len(".req.json")]
    res_path = bridge_dir / f"eval_{req_id}.res.json"
    try:
        payload = json.loads(claimed.read_text(encoding="utf-8"))
        if payload.get("language", "python") != "python":
            raise ValueError(f"Unsupported language: {payload.get('language')}")
        output = execute_python(
            str(payload.get("code") or ""),
            str(payload.get("repl_name") or "default"),
            bridge_dir,
            str(payload.get("token") or ""),
            runtime_dir,
        )
        atomic_write_json(res_path, {"ok": True, "output": output})
    except Exception as e:
        atomic_write_json(res_path, {"ok": False, "error": f"{type(e).__name__}: {e}"})
    finally:
        try:
            claimed.unlink()
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bridge-dir", default="/egg-bridge")
    parser.add_argument("--runtime-dir", default="/egg-runtime")
    parser.add_argument("--poll-sec", type=float, default=0.05)
    args = parser.parse_args()
    bridge_dir = Path(args.bridge_dir)
    runtime_dir = Path(args.runtime_dir)
    bridge_dir.mkdir(parents=True, exist_ok=True)
    while True:
        for req in sorted(bridge_dir.glob("eval_*.req.json")):
            process_eval_request(req, bridge_dir, runtime_dir)
        time.sleep(args.poll_sec)


if __name__ == "__main__":
    raise SystemExit(main())
