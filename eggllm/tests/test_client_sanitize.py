from egg_llm.client import LLMClient
from egg_llm.config import load_models_config
from egg_llm.catalog import AllModelsCatalog

from pathlib import Path
import json

# Minimal in-memory setup for sanitizer behavior via client internals

def test_library_constructs(tmp_path: Path):
    models = {
        "providers": {
            "openai": {
                "api_base": "https://api.openai.com/v1/chat/completions",
                "api_key_env": "OPENAI_API_KEY",
                "models": {"OpenAI GPT-4o": {"model_name": "gpt-4o"}}
            }
        }
    }
    mpath = tmp_path / "models.json"
    apath = tmp_path / "all-models.json"
    mpath.write_text(json.dumps(models))
    apath.write_text(json.dumps({"providers": {}}))

    client = LLMClient(models_path=mpath, all_models_path=apath)
    assert client.current_model_key in client.registry.models_config
