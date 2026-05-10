# eggllm

`eggllm` is a small, dependency-light LLM router used by Egg and usable on its
own. It reads plain JSON model/provider configuration and exposes a single
streaming chat interface over OpenAI-compatible providers.

It intentionally does not execute tools, render UI, or own conversation state.
Those responsibilities live in callers such as `eggthreads`.

## Features

- Plain `models.json` configuration for providers, model display names, aliases,
  and default parameters.
- Optional `all-models.json` provider catalog cache for `all:provider:model`
  selection.
- OpenAI-compatible chat and responses endpoint support.
- Streaming event interface for:
  - `content_delta`;
  - `reasoning_delta`;
  - `reasoning_summary_delta`;
  - `tool_calls_delta`;
  - final `done` messages.
- Parameter merging: provider defaults plus model overrides.
- Environment-variable API keys via `api_key_env`.

## Install

From the monorepo:

```bash
pip install -e ./eggllm
```

As a dependency from GitHub:

```text
eggllm @ git+https://github.com/albertvucinovic/egg-mono.git#subdirectory=eggllm
```

Runtime dependency: `requests`. Python 3.10+ is required.

## Configuration

`models.json` is the main configuration file:

```json
{
  "default_model": "OpenAI GPT-4o",
  "providers": {
    "openai": {
      "api_base": "https://api.openai.com/v1/chat/completions",
      "api_key_env": "OPENAI_API_KEY",
      "parameters": {
        "temperature": 0.2
      },
      "models": {
        "OpenAI GPT-4o": {
          "model_name": "gpt-4o",
          "alias": ["g4o"],
          "max_tokens": 128000,
          "parameters": {
            "max_output_tokens": 4096
          }
        }
      }
    }
  }
}
```

Important fields:

- `default_model`: optional initial model display key.
- `providers.<name>.api_base`: chat/completions or responses endpoint.
- `providers.<name>.api_key_env`: environment variable containing the key.
- `providers.<name>.parameters`: provider-level request defaults.
- `models.<display>.model_name`: provider API model id.
- `models.<display>.alias`: optional alternative names.
- `models.<display>.api_type`: optional adapter selection; supported values are
  `chat_completions` (default) and `responses`.
- `models.<display>.max_tokens`: model context-window length. Egg uses this for
  context budgeting/compaction threshold derivation.
- `models.<display>.parameters`: model-level request overrides.

Initial model selection precedence:

1. `EG_CHILD_MODEL` environment variable;
2. `DEFAULT_MODEL` environment variable;
3. `default_model` in `models.json`;
4. first configured model.

## Basic use

```python
from eggllm import LLMClient

llm = LLMClient(models_path="models.json", all_models_path="all-models.json")
llm.set_model("OpenAI GPT-4o")

messages = [
    {"role": "system", "content": "You are helpful."},
    {"role": "user", "content": "List three project risks."},
]

for event in llm.stream_chat(messages):
    if event["type"] == "content_delta":
        print(event["text"], end="", flush=True)
    elif event["type"] == "done":
        final_message = event["message"]
```

One-shot completion:

```python
final_message = llm.complete_chat(messages)
```

## Model selection

```python
llm.set_model("OpenAI GPT-4o")          # display name
llm.set_model("g4o")                    # alias
llm.set_model("openai:OpenAI GPT-4o")   # provider-qualified display name
llm.set_model("all:openrouter:qwen/qwen3-235b-a22b-thinking-2507")
```

List configured models/providers:

```python
print(llm.list_models_by_provider())
print(llm.get_providers())
```

## Provider catalogs

`all-models.json` caches provider catalog results for autocomplete and blind
`all:provider:model` selection:

```python
print(llm.update_all_models("openrouter"))
```

The catalog endpoint is derived by trimming common API suffixes such as
`/chat/completions`, `/completions`, or `/responses`, then appending `/models`.

## Tools/function calling

Pass OpenAI-style tool schemas. `eggllm` forwards schemas and streams tool-call
arguments; your application executes tools and appends `tool` messages.

```python
TOOLS = [{
    "type": "function",
    "function": {
        "name": "lookup",
        "description": "Look up a value",
        "parameters": {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"]
        }
    }
}]

for event in llm.stream_chat(messages, tools=TOOLS, tool_choice="auto"):
    if event["type"] == "tool_calls_delta":
        print(event["delta"])
```

The final assistant message may include stitched `tool_calls`.

## Reasoning events

Thinking/reasoning providers may stream:

- `reasoning_delta`: reasoning content that may need to be persisted depending
  on provider policy;
- `reasoning_summary_delta`: display-only summaries. Do not send these back as
  `reasoning_content`.

Egg's `eggthreads` runner handles provider-specific persistence policy for its
own conversations.

## Errors

- Construction raises `ValueError` when no usable models are configured.
- `set_model` raises `KeyError` for unknown model keys.
- HTTP/network failures raise `requests` exceptions.
- Provider stream failures propagate from the generator.

## Development

```bash
pip install -e "./eggllm[dev]"
pytest -q eggllm/tests
pyflakes eggllm/eggllm
```

## License

MIT, same as the monorepo.
