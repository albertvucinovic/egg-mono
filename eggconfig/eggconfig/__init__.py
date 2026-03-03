from pathlib import Path

_DATA_DIR = Path(__file__).parent / "data"


def get_models_path() -> Path:
    return _DATA_DIR / "models.json"


def get_all_models_path() -> Path:
    return _DATA_DIR / "all-models.json"
