# eggllm — Lightweight, OpenAI‑compatible LLM Router

eggllm is a small, dependency‑light library for routing chat requests to multiple
LLM providers using a single configuration. It borrows the spirit of libraries
like LiteLLM/Any‑LLM, but is purposely focused on:

- Keeping your configuration in plain JSON files (models.json and all-models.json)
- Seamless model switching by display name, alias, provider:name, or provider catalog (all:provider:model)
- OpenAI‑compatible streaming (SSE) with content, reasoning, and tool_calls deltas
- A headless core: no TUI/printing — you decide how to display/execute tool calls

This library is extracted from the Egg chat project and is used by it internally.
You can also use it directly in your own scripts.


## Features

- Single configuration source (models.json) for providers and models
- Optional provider catalogs cache (all-models.json) for “all:provider:model” selection
- Parameter merging: provider.parameters + model.parameters (model overrides provider)
- Environment‑based API keys per provider (api_key_env)
- Streaming generator interface that yields:
  - content_delta
  - reasoning_delta (for thinking models)
  - tool_calls_delta (function calling arguments as they stream)
  - done (with the final assistant message dict)


## Install

This library has minimal runtime dependencies and targets Python 3.10+.

- Required: requests

```
pip install requests
```

(If you are using the parent project, `requirements.txt` already includes requests.)


## File layout

```
eggllm/
  README.md
  __init__.py
  client.py              # LLMClient (main entry point)
  config.py              # load models.json
  catalog.py             # manage all-models.json
  registry.py            # model resolution and parameter merging
  providers/
    __init__.py
    base.py              # ProviderAdapter interface
    openai_compat.py     # OpenAI-compatible streaming adapter
```


## Configuration

### models.json (required)

The library reads your configuration from models.json in the following structure:

```
{
  "default_model": "OpenAI GPT-4o",           // optional
  "providers": {
    "openai": {
      "api_base": "https://api.openai.com/v1/chat/completions",
      "api_key_env": "OPENAI_API_KEY",
      "parameters": {                          // optional provider-level defaults
        "temperature": 0.2
      },
      "models": {
        "OpenAI GPT-4o": {                     // display name
          "model_name": "gpt-4o",
          "alias": ["g4o"],
          "parameters": {                      // optional model overrides
            "max_tokens": 4096
          }
        },
        "OpenAI o3": { "model_name": "o3-mini" }
      }
    },
    "openrouter": {
      "api_base": "https://openrouter.ai/api/v1/chat/completions",
      "api_key_env": "OPENROUTER_API_KEY",
      "models": {
        "OpenRouter Qwen3 235B": { "model_name": "qwen/qwen3-235b-a22b-thinking-2507" }
      }
    }
  }
}
```

- default_model: starting model key (matches a display name under any provider)
- providers: a map of provider name -> configuration
  - api_base: OpenAI‑compatible chat endpoint
  - api_key_env: name of the environment variable where the API key is stored
  - parameters: optional default parameters applied to all models of that provider
  - models: display name -> per-model config
    - model_name: the provider's API model id
    - alias: optional list of alternative names (strings)
    - parameters: optional per-model overrides

Environment variables: set one per provider configured with `api_key_env`.
For example, for the `openai` provider above, set OPENAI_API_KEY.

The initial model selection precedence is:

1. EG_CHILD_MODEL (env)
2. DEFAULT_MODEL (env)
3. default_model from models.json
4. the first configured model


### all-models.json (optional)

This file caches full provider catalogs to support “all:provider:model” selection
and autocomplete suggestions. It is maintained by the library when you call
`update_all_models(provider)`.

Basic structure:

```
{
  "providers": {
    "openrouter": {
      "fetched_at": 1712345678,
      "source": "https://openrouter.ai/api/v1/models",
      "models": [
        "openai/gpt-oss-120b",
        "qwen/qwen3-235b-a22b-thinking-2507",
        "..."
      ]
    }
  }
}
```

Note: The library will attempt to derive the “models” endpoint from `api_base` by
trimming `/chat/completions | /completions | /responses` and appending `/models`.
This works for most OpenAI‑compatible APIs.


## Core API

### Import and construct

```
from eggllm import LLMClient

llm = LLMClient(models_path="models.json", all_models_path="all-models.json")
```

- models_path: path to your models.json
- all_models_path: path to your all-models.json (will be created/updated as needed)


### Model selection

```
# By exact display name
llm.set_model("OpenAI GPT-4o")

# By alias
llm.set_model("g4o")

# By provider:name (display name or alias)
llm.set_model("openai:OpenAI GPT-4o")
llm.set_model("openai:g4o")

# By full provider catalog (requires all-models.json cache, but also allows blind selection)
llm.set_model("all:openrouter:openai/gpt-oss-120b")
```

You can list available configured models by provider:

```
print(llm.list_models_by_provider())
# {'openai': ['OpenAI GPT-4o', 'OpenAI o3'], 'openrouter': ['OpenRouter Qwen3 235B']}
```

And get a list of configured providers:

```
print(llm.get_providers())  # e.g. ['openai', 'openrouter']
```


### Update provider catalogs

```
# Fetch full model catalog for a provider and write all-models.json
print(llm.update_all_models("openrouter"))
# -> Updated all-models.json for provider 'openrouter' with N models.
```

After this, you may use `all:openrouter:<model_id>` to select any model id present
in the catalog without touching models.json.


### Messages format (OpenAI‑compatible)

Messages are a list of dicts in the usual format:

```
messages = [
  {"role": "system", "content": "You are a helpful assistant."},
  {"role": "user",   "content": "Write a short poem about the sea."}
]
```

The library also accepts `tool` messages in the list (OpenAI compatible). It will
strip internal keys like `local_tool` automatically. The library itself does not
execute tools — it only passes tool schemas to the provider and streams back
function call arguments.


### Streaming chat

```
from eggllm import LLMClient

llm = LLMClient("models.json", "all-models.json")
llm.set_model("OpenAI GPT-4o")

messages = [
  {"role": "system", "content": "You are a helpful assistant."},
  {"role": "user",   "content": "List five European cities to visit."}
]

for event in llm.stream_chat(messages):
    t = event.get("type")
    if t == "content_delta":
        print(event["text"], end="", flush=True)
    elif t == "reasoning_delta":
        # Optional: print to a separate "thinking" area
        pass
    elif t == "tool_calls_delta":
        # Optional: update your UI with streamed tool arguments
        pass
    elif t == "done":
        final = event["message"]
        print("\n---\nFinal message:", final)
```

The final assistant message dict may include:

- content (string)
- tool_calls (list of function calls with stitched id/name/arguments)
- reasoning_content (string, if the model produced it)

The library yields events as they arrive and raises exceptions for network or
provider errors. Wrap your loop in try/except if you need custom handling.


### One‑shot completion (no streaming)

```
final_msg = llm.complete_chat(messages)
print(final_msg)
```

This consumes the stream internally and returns the final assistant message dict.


### Using tools (function calling)

Pass OpenAI‑style tool schemas via the `tools` argument. The library does not
execute tools; it just forwards schemas and returns the streamed `tool_calls`.

```
TOOLS = [
  {
    "type": "function",
    "function": {
      "name": "bash",
      "description": "Run a bash command",
      "parameters": {
        "type": "object",
        "properties": {"script": {"type": "string"}},
        "required": ["script"]
      }
    }
  }
]

# Streaming with tools
for event in llm.stream_chat(messages, tools=TOOLS, tool_choice="auto"):
    if event["type"] == "tool_calls_delta":
        # Inspect event["delta"] to see the latest arguments
        pass
    elif event["type"] == "done":
        assistant = event["message"]
        if assistant.get("tool_calls"):
            # Your code should execute the tool(s), produce tool outputs, then
            # append them as messages and continue the conversation.
            pass
```


### Send context only (no visible reply)

Sends a one‑off user message to enrich the context without collecting output:

```
llm.send_context_only(messages, "[SYSTEM NOTE] Document index built.")
```

Under the hood this posts with `stream=False`, `max_tokens=1`.


## Parameter merging and headers

- Parameters sent in the request are merged as:
  - provider.parameters (from models.json)
  - then overridden by model.parameters
- Headers include `Authorization: Bearer <API_KEY>` if the provider defines
  `api_key_env` and the corresponding environment variable is present.

To inspect the current provider and headers (rarely needed):

```
provider_name, url, headers = llm.current_provider_and_url()
```


## Error handling

- Construction raises `ValueError` if no models are configured
- `set_model` raises `KeyError` on unknown keys
- HTTP failures raise `requests` exceptions (e.g., `RequestException`)
- The streaming generator yields events until done, or raises on failures

Wrap calls in try/except if you want to handle errors explicitly.


## Provider support

eggllm currently targets OpenAI‑compatible chat endpoints (`/v1/chat/completions`).
This covers many providers and gateways (OpenAI, OpenRouter, Groq, local proxies, etc.).

Additional adapters for non‑OpenAI APIs (e.g., Anthropic native, Google native)
can be added behind `ProviderAdapter` without changing your integrations.


## Tips & patterns

- Use `llm.update_all_models(provider)` to cache provider catalogs for better
  discoverability; then pick using `all:provider:model`.
- Keep sensitive keys in environment variables, not in JSON files.
- Set `DEFAULT_MODEL` or `EG_CHILD_MODEL` as environment variables to pin the
  initial model without modifying JSON.


## License

Same license as this repository.
