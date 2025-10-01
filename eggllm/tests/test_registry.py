from egg_llm.registry import ModelRegistry
from egg_llm.catalog import AllModelsCatalog

class DummyCatalog:
    def __init__(self, models):
        self._models = models
    def get_all_models_for_provider(self, p):
        return self._models.get(p, [])


def test_resolve_display_and_alias():
    models = {
        "OpenAI GPT-4o": {"provider": "openai", "model_name": "gpt-4o", "alias": ["g4o"]},
        "Other": {"provider": "openai", "model_name": "other"},
    }
    providers = {"openai": {"api_base": "x", "api_key_env": "OPENAI_API_KEY"}}
    r = ModelRegistry(models, providers, DummyCatalog({}))
    assert r.resolve("OpenAI GPT-4o") == "OpenAI GPT-4o"
    assert r.resolve("g4o") == "OpenAI GPT-4o"
    assert r.resolve("openai:OpenAI GPT-4o") == "OpenAI GPT-4o"


def test_resolve_all_provider_model():
    models = {}
    providers = {"openai": {"api_base": "x", "api_key_env": "OPENAI_API_KEY"}}
    r = ModelRegistry(models, providers, DummyCatalog({"openai": ["gpt-4o"]}))
    key = r.resolve("all:openai:gpt-4o")
    assert key == "all:openai:gpt-4o"
    assert r.get_model_config(key)["model_name"] == "gpt-4o"
