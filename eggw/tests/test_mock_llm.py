"""Contract tests for EggW's deterministic browser-test LLM adapter."""

from eggw.mock_llm import MockLLMClient


def test_mock_llm_supports_runner_model_selection_contract():
    client = MockLLMClient()

    assert client.set_model("mock:model") == "mock:model"
    assert client.current_model_key == "mock:model"
    assert client.set_model_with_config("mock:configured", {"provider": "mock"}) == "mock:configured"
    assert client.current_model_key == "mock:configured"
