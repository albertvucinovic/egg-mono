"""Single-source EggW browser theme registry shared with the frontend."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

_CONTRACT_PATH = Path(__file__).with_name("theme-contract.json")


@lru_cache(maxsize=1)
def theme_contract() -> dict[str, Any]:
    return json.loads(_CONTRACT_PATH.read_text(encoding="utf-8"))


THEMES = tuple(theme["name"] for theme in theme_contract()["themes"])
THEME_METADATA = {theme["name"]: theme for theme in theme_contract()["themes"]}
DEFAULT_THEME = str(theme_contract()["defaultTheme"])


def normalize_theme_name(name: str | None) -> str:
    candidate = (name or "").strip().lower()
    return candidate if candidate in THEME_METADATA else DEFAULT_THEME
