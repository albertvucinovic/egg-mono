"""Tests for OpenAI Responses API adapter and factory."""

import json
from pathlib import Path

import pytest

from eggllm.providers.openai_responses import OpenAIResponsesAdapter
from eggllm.providers.factory import AdapterFactory
from eggllm.providers.openai_compat import OpenAICompatAdapter
from eggllm.client import LLMClient


class TestMessageConversion:
    """Tests for converting Chat Completions messages to Responses API input."""

    def setup_method(self):
        self.adapter = OpenAIResponsesAdapter()

    def test_system_message_becomes_instructions(self):
        """System message should be extracted as 'instructions' field."""
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello"},
        ]
        instructions, input_items = self.adapter._convert_messages_to_input(messages)

        assert instructions == "You are a helpful assistant."
        assert len(input_items) == 1
        assert input_items[0]["role"] == "user"
        assert input_items[0]["content"] == "Hello"

    def test_user_assistant_messages(self):
        """User and assistant messages should become input items."""
        messages = [
            {"role": "user", "content": "What is 2+2?"},
            {"role": "assistant", "content": "2+2 equals 4."},
            {"role": "user", "content": "Thanks!"},
        ]
        instructions, input_items = self.adapter._convert_messages_to_input(messages)

        assert instructions is None
        assert len(input_items) == 3
        assert input_items[0] == {"type": "message", "role": "user", "content": "What is 2+2?"}
        assert input_items[1] == {"type": "message", "role": "assistant", "content": "2+2 equals 4."}
        assert input_items[2] == {"type": "message", "role": "user", "content": "Thanks!"}

    def test_tool_calls_conversion(self):
        """Assistant tool calls should become function_call items."""
        messages = [
            {"role": "user", "content": "Search for cats"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_123",
                        "type": "function",
                        "function": {
                            "name": "web_search",
                            "arguments": '{"query": "cats"}'
                        }
                    }
                ]
            },
        ]
        instructions, input_items = self.adapter._convert_messages_to_input(messages)

        assert len(input_items) == 2
        assert input_items[1]["type"] == "function_call"
        assert input_items[1]["id"] == "call_123"
        assert input_items[1]["name"] == "web_search"
        assert input_items[1]["arguments"] == '{"query": "cats"}'

    def test_tool_result_conversion(self):
        """Tool results should become function_call_output items."""
        messages = [
            {"role": "user", "content": "Search for cats"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_123",
                        "type": "function",
                        "function": {"name": "web_search", "arguments": '{"query": "cats"}'}
                    }
                ]
            },
            {
                "role": "tool",
                "tool_call_id": "call_123",
                "content": "Found 10 results about cats."
            },
        ]
        instructions, input_items = self.adapter._convert_messages_to_input(messages)

        assert len(input_items) == 3
        assert input_items[2]["type"] == "function_call_output"
        assert input_items[2]["call_id"] == "call_123"
        assert input_items[2]["output"] == "Found 10 results about cats."

    def test_content_array_in_system_message(self):
        """System message with content array should be joined as text."""
        messages = [
            {
                "role": "system",
                "content": [
                    {"type": "text", "text": "Part 1."},
                    {"type": "text", "text": "Part 2."},
                ]
            },
            {"role": "user", "content": "Hello"},
        ]
        instructions, input_items = self.adapter._convert_messages_to_input(messages)

        assert instructions == "Part 1.\nPart 2."


class TestToolConversion:
    """Tests for converting Chat Completions tools to Responses API format."""

    def setup_method(self):
        self.adapter = OpenAIResponsesAdapter()

    def test_function_tool_conversion(self):
        """Chat Completions function tools should be flattened."""
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get current weather",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "location": {"type": "string"}
                        },
                        "required": ["location"]
                    }
                }
            }
        ]
        converted = self.adapter._convert_tools_to_responses_format(tools)

        assert len(converted) == 1
        assert converted[0]["type"] == "function"
        assert converted[0]["name"] == "get_weather"
        assert converted[0]["description"] == "Get current weather"
        assert "parameters" in converted[0]
        assert "function" not in converted[0]  # Should be flattened

    def test_builtin_tool_passthrough(self):
        """Built-in tools like web_search_preview should pass through unchanged."""
        tools = [
            {"type": "web_search_preview"},
            {"type": "code_interpreter"},
        ]
        converted = self.adapter._convert_tools_to_responses_format(tools)

        assert converted == tools

    def test_mixed_tools(self):
        """Mix of function tools and built-in tools."""
        tools = [
            {"type": "web_search_preview"},
            {
                "type": "function",
                "function": {
                    "name": "calculate",
                    "description": "Do math",
                    "parameters": {"type": "object", "properties": {}}
                }
            }
        ]
        converted = self.adapter._convert_tools_to_responses_format(tools)

        assert len(converted) == 2
        assert converted[0] == {"type": "web_search_preview"}
        assert converted[1]["name"] == "calculate"
        assert "function" not in converted[1]

    def test_strict_parameter_preserved(self):
        """The 'strict' parameter should be preserved."""
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "strict_func",
                    "parameters": {"type": "object"},
                    "strict": True
                }
            }
        ]
        converted = self.adapter._convert_tools_to_responses_format(tools)

        assert converted[0]["strict"] is True


class TestPayloadBuilding:
    """Tests for building Responses API payload."""

    def setup_method(self):
        self.adapter = OpenAIResponsesAdapter()

    def test_basic_payload(self):
        """Basic payload should include model, input, and stream."""
        original = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Hello"}],
            "stream": True,
        }
        payload = self.adapter._build_payload(original)

        assert payload["model"] == "gpt-4o"
        assert payload["stream"] is True
        assert len(payload["input"]) == 1
        assert "messages" not in payload

    def test_payload_with_system_instructions(self):
        """System message should become instructions field."""
        original = {
            "model": "gpt-4o",
            "messages": [
                {"role": "system", "content": "Be helpful."},
                {"role": "user", "content": "Hello"},
            ],
            "stream": True,
        }
        payload = self.adapter._build_payload(original)

        assert payload["instructions"] == "Be helpful."
        assert len(payload["input"]) == 1

    def test_payload_with_builtin_tools(self):
        """Built-in tools should be passed through unchanged."""
        original = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Hello"}],
            "tools": [{"type": "web_search_preview"}],
            "stream": True,
        }
        payload = self.adapter._build_payload(original)

        assert payload["tools"] == [{"type": "web_search_preview"}]

    def test_payload_with_function_tools_converted(self):
        """Function tools should be converted to Responses API format."""
        original = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Hello"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "Get weather",
                        "parameters": {"type": "object"}
                    }
                }
            ],
            "stream": True,
        }
        payload = self.adapter._build_payload(original)

        assert len(payload["tools"]) == 1
        assert payload["tools"][0]["name"] == "get_weather"
        assert "function" not in payload["tools"][0]

    def test_max_tokens_becomes_max_output_tokens(self):
        """max_tokens should be converted to max_output_tokens."""
        original = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Hello"}],
            "max_tokens": 1000,
            "stream": True,
        }
        payload = self.adapter._build_payload(original)

        assert "max_tokens" not in payload
        assert payload["max_output_tokens"] == 1000

    def test_temperature_passed_through(self):
        """Temperature should be passed through."""
        original = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Hello"}],
            "temperature": 0.7,
            "stream": True,
        }
        payload = self.adapter._build_payload(original)

        assert payload["temperature"] == 0.7


class TestAdapterFactory:
    """Tests for AdapterFactory."""

    def test_default_is_chat_completions(self):
        """Default adapter should be OpenAICompatAdapter."""
        adapter = AdapterFactory.get_adapter()
        assert isinstance(adapter, OpenAICompatAdapter)

    def test_chat_completions_type(self):
        """'chat_completions' should return OpenAICompatAdapter."""
        adapter = AdapterFactory.get_adapter("chat_completions")
        assert isinstance(adapter, OpenAICompatAdapter)

    def test_responses_type(self):
        """'responses' should return OpenAIResponsesAdapter."""
        adapter = AdapterFactory.get_adapter("responses")
        assert isinstance(adapter, OpenAIResponsesAdapter)

    def test_unknown_type_raises(self):
        """Unknown api_type should raise ValueError."""
        with pytest.raises(ValueError) as exc_info:
            AdapterFactory.get_adapter("unknown_api")
        assert "unknown_api" in str(exc_info.value)

    def test_singleton_behavior(self):
        """Same adapter instance should be returned for same type."""
        adapter1 = AdapterFactory.get_adapter("chat_completions")
        adapter2 = AdapterFactory.get_adapter("chat_completions")
        assert adapter1 is adapter2

    def test_supported_types(self):
        """supported_types should list all available types."""
        types = AdapterFactory.supported_types()
        assert "chat_completions" in types
        assert "responses" in types


class TestClientModelApiBase:
    """Tests for model-level api_base override in LLMClient."""

    def test_model_api_base_override(self, tmp_path: Path):
        """Model-level api_base should override provider-level."""
        models = {
            "providers": {
                "openai": {
                    "api_base": "https://api.openai.com/v1/chat/completions",
                    "api_key_env": "OPENAI_API_KEY",
                    "models": {
                        "GPT-4o": {"model_name": "gpt-4o"},
                        "GPT-4o Responses": {
                            "model_name": "gpt-4o",
                            "api_type": "responses",
                            "api_base": "https://api.openai.com/v1/responses"
                        }
                    }
                }
            }
        }
        mpath = tmp_path / "models.json"
        apath = tmp_path / "all-models.json"
        mpath.write_text(json.dumps(models))
        apath.write_text(json.dumps({"providers": {}}))

        import os
        os.environ["OPENAI_API_KEY"] = "test-key"

        client = LLMClient(models_path=mpath, all_models_path=apath)

        # Default model uses provider-level api_base
        client.set_model("GPT-4o")
        _, url, _ = client.current_provider_and_url()
        assert url == "https://api.openai.com/v1/chat/completions"

        # Responses model uses model-level api_base
        client.set_model("GPT-4o Responses")
        _, url, _ = client.current_provider_and_url()
        assert url == "https://api.openai.com/v1/responses"

    def test_auto_url_rewrite_for_responses_api(self, tmp_path: Path):
        """Setting api_type: responses should auto-rewrite chat/completions URL."""
        models = {
            "providers": {
                "openai": {
                    "api_base": "https://api.openai.com/v1/chat/completions",
                    "api_key_env": "OPENAI_API_KEY",
                    "models": {
                        "GPT-4o": {"model_name": "gpt-4o"},
                        "GPT-4o Responses": {
                            "model_name": "gpt-4o",
                            "api_type": "responses"
                            # Note: no api_base specified!
                        }
                    }
                }
            }
        }
        mpath = tmp_path / "models.json"
        apath = tmp_path / "all-models.json"
        mpath.write_text(json.dumps(models))
        apath.write_text(json.dumps({"providers": {}}))

        import os
        os.environ["OPENAI_API_KEY"] = "test-key"

        client = LLMClient(models_path=mpath, all_models_path=apath)

        # Default model uses original URL
        client.set_model("GPT-4o")
        _, url, _ = client.current_provider_and_url()
        assert url == "https://api.openai.com/v1/chat/completions"

        # Responses model auto-rewrites URL
        client.set_model("GPT-4o Responses")
        _, url, _ = client.current_provider_and_url()
        assert url == "https://api.openai.com/v1/responses"

    def test_explicit_api_base_not_rewritten(self, tmp_path: Path):
        """Explicit model-level api_base should not be rewritten."""
        models = {
            "providers": {
                "openai": {
                    "api_base": "https://api.openai.com/v1/chat/completions",
                    "api_key_env": "OPENAI_API_KEY",
                    "models": {
                        "Custom Endpoint": {
                            "model_name": "gpt-4o",
                            "api_type": "responses",
                            "api_base": "https://custom.example.com/api/v2/responses"
                        }
                    }
                }
            }
        }
        mpath = tmp_path / "models.json"
        apath = tmp_path / "all-models.json"
        mpath.write_text(json.dumps(models))
        apath.write_text(json.dumps({"providers": {}}))

        import os
        os.environ["OPENAI_API_KEY"] = "test-key"

        client = LLMClient(models_path=mpath, all_models_path=apath)
        client.set_model("Custom Endpoint")
        _, url, _ = client.current_provider_and_url()
        # Should use explicit URL, not rewrite provider URL
        assert url == "https://custom.example.com/api/v2/responses"


class TestUrlRewriting:
    """Tests for URL rewriting between API types."""

    def test_rewrite_chat_completions_to_responses(self, tmp_path: Path):
        """Should rewrite /chat/completions to /responses."""
        models = {
            "providers": {
                "openai": {
                    "api_base": "https://api.openai.com/v1/chat/completions",
                    "api_key_env": "OPENAI_API_KEY",
                    "models": {"Test": {"model_name": "test"}}
                }
            }
        }
        mpath = tmp_path / "models.json"
        apath = tmp_path / "all-models.json"
        mpath.write_text(json.dumps(models))
        apath.write_text(json.dumps({"providers": {}}))

        import os
        os.environ["OPENAI_API_KEY"] = "test-key"

        client = LLMClient(models_path=mpath, all_models_path=apath)

        # Test various URL formats
        assert client._rewrite_url_for_api_type(
            "https://api.openai.com/v1/chat/completions", "responses"
        ) == "https://api.openai.com/v1/responses"

        assert client._rewrite_url_for_api_type(
            "https://localhost:8080/v1/chat/completions", "responses"
        ) == "https://localhost:8080/v1/responses"

        # URL ending with /v1 should append /responses
        assert client._rewrite_url_for_api_type(
            "https://api.openai.com/v1", "responses"
        ) == "https://api.openai.com/v1/responses"

    def test_rewrite_responses_to_chat_completions(self, tmp_path: Path):
        """Should rewrite /responses to /chat/completions."""
        models = {
            "providers": {
                "openai": {
                    "api_base": "https://api.openai.com/v1/responses",
                    "api_key_env": "OPENAI_API_KEY",
                    "models": {"Test": {"model_name": "test"}}
                }
            }
        }
        mpath = tmp_path / "models.json"
        apath = tmp_path / "all-models.json"
        mpath.write_text(json.dumps(models))
        apath.write_text(json.dumps({"providers": {}}))

        import os
        os.environ["OPENAI_API_KEY"] = "test-key"

        client = LLMClient(models_path=mpath, all_models_path=apath)

        assert client._rewrite_url_for_api_type(
            "https://api.openai.com/v1/responses", "chat_completions"
        ) == "https://api.openai.com/v1/chat/completions"

    def test_no_rewrite_when_pattern_not_found(self, tmp_path: Path):
        """Should return original URL if pattern not found."""
        models = {
            "providers": {
                "custom": {
                    "api_base": "https://custom.example.com/api",
                    "api_key_env": "CUSTOM_KEY",
                    "models": {"Test": {"model_name": "test"}}
                }
            }
        }
        mpath = tmp_path / "models.json"
        apath = tmp_path / "all-models.json"
        mpath.write_text(json.dumps(models))
        apath.write_text(json.dumps({"providers": {}}))

        import os
        os.environ["CUSTOM_KEY"] = "test-key"

        client = LLMClient(models_path=mpath, all_models_path=apath)

        # URL without recognized pattern should be unchanged
        assert client._rewrite_url_for_api_type(
            "https://custom.example.com/api", "responses"
        ) == "https://custom.example.com/api"

    def test_adapter_selection_by_api_type(self, tmp_path: Path):
        """Client should select correct adapter based on model's api_type."""
        models = {
            "providers": {
                "openai": {
                    "api_base": "https://api.openai.com/v1/chat/completions",
                    "api_key_env": "OPENAI_API_KEY",
                    "models": {
                        "GPT-4o": {"model_name": "gpt-4o"},
                        "GPT-4o Responses": {
                            "model_name": "gpt-4o",
                            "api_type": "responses",
                            "api_base": "https://api.openai.com/v1/responses"
                        }
                    }
                }
            }
        }
        mpath = tmp_path / "models.json"
        apath = tmp_path / "all-models.json"
        mpath.write_text(json.dumps(models))
        apath.write_text(json.dumps({"providers": {}}))

        import os
        os.environ["OPENAI_API_KEY"] = "test-key"

        client = LLMClient(models_path=mpath, all_models_path=apath)

        # Default model uses OpenAICompatAdapter
        client.set_model("GPT-4o")
        adapter = client._get_adapter_for_current_model()
        assert isinstance(adapter, OpenAICompatAdapter)

        # Responses model uses OpenAIResponsesAdapter
        client.set_model("GPT-4o Responses")
        adapter = client._get_adapter_for_current_model()
        assert isinstance(adapter, OpenAIResponsesAdapter)
