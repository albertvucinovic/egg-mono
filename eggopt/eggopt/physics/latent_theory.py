from __future__ import annotations

import json
from pathlib import Path


def evaluator_source() -> str:
    """Return the standard-library-only latent Physics evaluator source."""

    return Path(__file__).with_name("standalone_latent.py").read_text()


def evaluator_file_script(request_path: str) -> str:
    """Load a latent evaluator request and world model from durable files."""

    request_literal = repr(str(request_path))
    loader = (
        "import json\n"
        "from pathlib import Path\n"
        f"_EGG_PHYSICS_REQUEST = json.loads(Path({request_literal}).read_text())\n"
        '_EGG_PHYSICS_REQUEST["source"] = Path('
        '_EGG_PHYSICS_REQUEST.pop("source_path")).read_text()\n'
        '_EGG_PHYSICS_REQUEST["timeline"] = json.loads(Path('
        '_EGG_PHYSICS_REQUEST.pop("timeline_path")).read_text())\n'
        'plan_path = _EGG_PHYSICS_REQUEST.pop("plan_path", None)\n'
        'if plan_path is not None: _EGG_PHYSICS_REQUEST["plan"] = '
        'json.loads(Path(plan_path).read_text())\n'
    )
    return loader + evaluator_source()


def parse_evaluator_receipt(output: str) -> str:
    marker = "__EGG_PHYSICS_LATENT_REPORT__"
    line = next(
        (line for line in reversed(str(output).splitlines()) if marker in line), None
    )
    if line is None:
        raise ValueError(f"trusted latent evaluator did not write its report:\n{output}")
    try:
        path = json.loads(line.split(marker, 1)[1])["path"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError("trusted latent evaluator returned an invalid receipt") from exc
    if not isinstance(path, str) or not path:
        raise ValueError("trusted latent evaluator returned an invalid report path")
    return path


__all__ = ["evaluator_file_script", "evaluator_source", "parse_evaluator_receipt"]
