from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def evaluator_source() -> str:
    """Return the canonical standard-library-only Physics evaluator source."""

    return (Path(__file__).with_name("standalone_plan.py")).read_text()


def evaluator_script(request: dict[str, Any]) -> str:
    """Return a self-contained trusted evaluator script for ``python_exec``."""

    payload = json.dumps(
        request, allow_nan=False, separators=(",", ":"), sort_keys=True
    )
    return f"import json\n_EGG_PHYSICS_REQUEST = json.loads({payload!r})\n" + evaluator_source()


def evaluator_file_script(request_path: str) -> str:
    """Return a compact evaluator that loads its potentially large inputs from files."""

    request_literal = repr(str(request_path))
    loader = (
        "import json\n"
        "from pathlib import Path\n"
        f"_EGG_PHYSICS_REQUEST = json.loads(Path({request_literal}).read_text())\n"
        '_EGG_PHYSICS_REQUEST["source"] = Path(_EGG_PHYSICS_REQUEST.pop("source_path")).read_text()\n'
        'timeline = json.loads(Path(_EGG_PHYSICS_REQUEST.pop("timeline_path")).read_text())\n'
        '_EGG_PHYSICS_REQUEST["timeline"] = timeline["timeline"]\n'
        '_EGG_PHYSICS_REQUEST["plan"] = json.loads(Path(_EGG_PHYSICS_REQUEST.pop("plan_path")).read_text())\n'
    )
    return loader + evaluator_source()


def parse_evaluator_output(output: str) -> dict[str, Any]:
    marker = "__EGG_PHYSICS_RESULT__"
    line = next(
        (line for line in reversed(str(output).splitlines()) if marker in line), None
    )
    if line is None:
        raise ValueError(f"trusted model evaluator failed:\n{output}")
    try:
        return json.loads(line.split(marker, 1)[1])
    except json.JSONDecodeError as exc:
        raise ValueError("trusted model evaluator returned invalid JSON") from exc


def parse_evaluator_receipt(output: str) -> str:
    marker = "__EGG_PHYSICS_REPORT__"
    line = next(
        (line for line in reversed(str(output).splitlines()) if marker in line), None
    )
    if line is None:
        raise ValueError(f"trusted model evaluator did not write its report:\n{output}")
    try:
        path = json.loads(line.split(marker, 1)[1])["path"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError("trusted model evaluator returned an invalid receipt") from exc
    if not isinstance(path, str) or not path:
        raise ValueError("trusted model evaluator returned an invalid report path")
    return path


__all__ = [
    "evaluator_file_script",
    "evaluator_script",
    "evaluator_source",
    "parse_evaluator_output",
    "parse_evaluator_receipt",
]
