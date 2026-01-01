from eggllm.registry import ModelRegistry
from eggllm.catalog import AllModelsCatalog

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


def test_get_concrete_model_info():
    models = {
        "OpenAI GPT-4o": {"provider": "openai", "model_name": "gpt-4o", "max_tokens": 4096}
    }
    providers = {
        "openai": {
            "api_base": "https://api.openai.com/v1/chat/completions",
            "api_key_env": "OPENAI_API_KEY",
            "parameters": {"temperature": 0.7}
        }
    }
    r = ModelRegistry(models, providers, DummyCatalog({}))
    info = r.get_concrete_model_info("OpenAI GPT-4o")
    expected = {
        "providers": {
            "openai": {
                "api_base": "https://api.openai.com/v1/chat/completions",
                "api_key_env": "OPENAI_API_KEY",
                "parameters": {"temperature": 0.7},
                "models": {
                    "OpenAI GPT-4o": {
                        "model_name": "gpt-4o",
                        "max_tokens": 4096
                    }
                }
            }
        }
    }
    assert info == expected


def test_add_ephemeral_from_concrete_info():
    models = {}
    providers = {}
    r = ModelRegistry(models, providers, DummyCatalog({}))
    concrete = {
        "providers": {
            "openai": {
                "api_base": "https://api.openai.com/v1/chat/completions",
                "api_key_env": "OPENAI_API_KEY",
                "models": {
                    "MyCustomModel": {
                        "model_name": "custom-gpt",
                        "max_tokens": 2048
                    }
                }
            }
        }
    }
    # Add with explicit display key
    key = r.add_ephemeral_from_concrete_info(concrete, display_key="MyModel")
    assert key == "MyModel"
    # Should now be resolvable
    resolved = r.resolve("MyModel")
    assert resolved == "MyModel"
    config = r.get_model_config("MyModel")
    assert config["provider"] == "openai"
    assert config["model_name"] == "custom-gpt"
    assert config["max_tokens"] == 2048
    # Provider config should be added
    prov_cfg = r.provider_config("openai")
    assert prov_cfg["api_base"] == "https://api.openai.com/v1/chat/completions"
    assert prov_cfg["api_key_env"] == "OPENAI_API_KEY"


def test_add_ephemeral_from_concrete_info_no_display_key():
    models = {}
    providers = {}
    r = ModelRegistry(models, providers, DummyCatalog({}))
    concrete = {
        "providers": {
            "openai": {
                "api_base": "https://api.openai.com/v1/chat/completions",
                "api_key_env": "OPENAI_API_KEY",
                "models": {
                    "MyCustomModel": {
                        "model_name": "custom-gpt"
                    }
                }
            }
        }
    }
    # No display_key -> use the key from models dict
    key = r.add_ephemeral_from_concrete_info(concrete)
    assert key == "MyCustomModel"
    assert r.resolve("MyCustomModel") == "MyCustomModel"
