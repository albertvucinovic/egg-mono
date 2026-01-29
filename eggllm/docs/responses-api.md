# OpenAI Responses API vs Chat Completions API

This document explains how eggllm supports both OpenAI's Chat Completions API and the newer Responses API, including configuration, format differences, and implementation details.

## Overview

OpenAI offers two APIs for conversational AI:

| Aspect | Chat Completions | Responses API |
|--------|------------------|---------------|
| Endpoint | `/v1/chat/completions` | `/v1/responses` |
| Released | 2023 | March 2025 |
| Input format | `messages` array | `input` items + `instructions` |
| Tool calls | `tool_calls` array | `function_call` items |
| Built-in tools | None | `web_search`, `code_interpreter`, `file_search` |

The Responses API is OpenAI's newest interface, combining strengths of Chat Completions and Assistants APIs into a streamlined experience.

## Configuration

eggllm supports both APIs through model-level configuration. Each model can specify its own `api_type`:

```json
{
  "providers": {
    "openai": {
      "api_base": "https://api.openai.com/v1/chat/completions",
      "api_key_env": "OPENAI_API_KEY",
      "models": {
        "GPT-4o": {
          "model_name": "gpt-4o"
        },
        "GPT-4o Responses": {
          "model_name": "gpt-4o",
          "api_type": "responses"
        },
        "GPT-4o with WebSearch": {
          "model_name": "gpt-4o",
          "api_type": "responses",
          "parameters": {
            "tools": [{"type": "web_search_preview"}]
          }
        }
      }
    }
  }
}
```

### Configuration Options

| Field | Description | Default |
|-------|-------------|---------|
| `api_type` | `"chat_completions"` or `"responses"` | `"chat_completions"` |
| `api_base` | Model-level URL override (optional) | Uses provider's `api_base` |

### Automatic URL Rewriting

When `api_type: "responses"` is set without an explicit `api_base`, eggllm automatically rewrites the provider's URL:

```
https://api.openai.com/v1/chat/completions
                    ↓
https://api.openai.com/v1/responses
```

This means you only need to set `api_type: "responses"` - no need to duplicate URLs.

## Format Differences

### Messages vs Items

**Chat Completions** uses a `messages` array with role-based messages:

```json
{
  "messages": [
    {"role": "system", "content": "You are helpful."},
    {"role": "user", "content": "Hello"},
    {"role": "assistant", "content": "Hi there!"}
  ]
}
```

**Responses API** uses `input` items with an `instructions` field:

```json
{
  "instructions": "You are helpful.",
  "input": [
    {"type": "message", "role": "user", "content": "Hello"},
    {"type": "message", "role": "assistant", "content": "Hi there!"}
  ]
}
```

### Tool Definitions

**Chat Completions** nests function details:

```json
{
  "tools": [{
    "type": "function",
    "function": {
      "name": "get_weather",
      "description": "Get weather for a city",
      "parameters": {"type": "object", "properties": {...}}
    }
  }]
}
```

**Responses API** flattens the structure:

```json
{
  "tools": [{
    "type": "function",
    "name": "get_weather",
    "description": "Get weather for a city",
    "parameters": {"type": "object", "properties": {...}}
  }]
}
```

### Tool Calls and Results

This is the most critical difference and a common source of errors.

**Chat Completions:**

```json
// Assistant's tool call
{
  "role": "assistant",
  "tool_calls": [{
    "id": "call_abc123",
    "type": "function",
    "function": {"name": "get_weather", "arguments": "{\"city\":\"NYC\"}"}
  }]
}

// Tool result
{
  "role": "tool",
  "tool_call_id": "call_abc123",
  "content": "72°F, sunny"
}
```

**Responses API:**

```json
// Function call (separate item, not nested in message)
{
  "type": "function_call",
  "call_id": "call_abc123",
  "name": "get_weather",
  "arguments": "{\"city\":\"NYC\"}"
}

// Function call output
{
  "type": "function_call_output",
  "call_id": "call_abc123",
  "output": "72°F, sunny"
}
```

**Key difference:** The Responses API uses `call_id` (not `id`) and items are top-level (not nested in messages).

### Parallel Tool Calls

Both APIs support parallel tool calls. When the model calls multiple functions:

**Chat Completions:**
```json
{
  "role": "assistant",
  "tool_calls": [
    {"id": "call_abc", "function": {"name": "get_weather", "arguments": "..."}},
    {"id": "call_def", "function": {"name": "get_weather", "arguments": "..."}}
  ]
}
// Followed by two tool messages with matching tool_call_id
```

**Responses API:**
```json
// Two separate function_call items
{"type": "function_call", "call_id": "call_abc", "name": "get_weather", ...}
{"type": "function_call", "call_id": "call_def", "name": "get_weather", ...}

// Two separate function_call_output items
{"type": "function_call_output", "call_id": "call_abc", "output": "..."}
{"type": "function_call_output", "call_id": "call_def", "output": "..."}
```

## Streaming Events

### Chat Completions Streaming

Uses `choices[0].delta` structure:

```
data: {"choices":[{"delta":{"content":"Hello"}}]}
data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{"}}]}}]}
data: [DONE]
```

### Responses API Streaming

Uses typed events:

```
data: {"type":"response.output_item.added","item":{"type":"function_call","call_id":"call_abc",...}}
data: {"type":"response.function_call_arguments.delta","delta":"{\"city\""}
data: {"type":"response.function_call_arguments.done","arguments":"{\"city\":\"NYC\"}"}
data: {"type":"response.output_item.done","item":{...complete item...}}
data: {"type":"response.completed"}
```

**Important:** The `response.output_item.done` event contains the complete function call with the final `call_id` - essential for reliable ID capture.

## eggllm Architecture

### Adapter Pattern

eggllm uses the adapter pattern to abstract API differences:

```
┌─────────────────┐     ┌──────────────────────┐
│   LLMClient     │────▶│   AdapterFactory     │
└─────────────────┘     └──────────────────────┘
                                  │
                    ┌─────────────┴─────────────┐
                    ▼                           ▼
        ┌───────────────────┐       ┌───────────────────────┐
        │ OpenAICompatAdapter│       │ OpenAIResponsesAdapter │
        │ (Chat Completions) │       │   (Responses API)      │
        └───────────────────┘       └───────────────────────┘
```

### Normalized Event Stream

Both adapters emit the same normalized events, making the API transparent to consumers:

| Event Type | Description |
|------------|-------------|
| `content_delta` | Text content chunk |
| `reasoning_delta` | Reasoning/thinking content |
| `tool_calls_delta` | Updated tool calls state |
| `done` | Stream complete with final message |

### Conversion Flow

```
Chat Completions Format (from eggthreads)
            │
            ▼
┌───────────────────────────────────┐
│  OpenAIResponsesAdapter           │
│  ._convert_messages_to_input()    │
│  ._convert_tools_to_responses()   │
│  ._build_payload()                │
└───────────────────────────────────┘
            │
            ▼
    Responses API Format
            │
            ▼
      OpenAI API
            │
            ▼
    Responses API Events
            │
            ▼
┌───────────────────────────────────┐
│  Stream parsing & normalization   │
│  (call_id → id mapping)           │
└───────────────────────────────────┘
            │
            ▼
Normalized Events (to eggthreads)
```

## Common Errors and Solutions

### "Missing required parameter: 'tools[0].name'"

**Cause:** Sending Chat Completions tool format to Responses API.

**Solution:** eggllm automatically converts `{"function": {"name": "x"}}` to `{"name": "x"}`.

### "Missing required parameter: 'input[N].call_id'"

**Cause:** Using `id` instead of `call_id` for function_call items.

**Solution:** eggllm uses `call_id` field for Responses API function_call and function_call_output items.

### "No tool output found for function call"

**Cause:** `call_id` in function_call_output doesn't match the function_call's `call_id`.

**Solution:** eggllm captures `call_id` from streaming events (preferring `call_id` over `id`) and maintains consistent ID mapping.

## Built-in Tools (Responses API Only)

The Responses API supports built-in tools that don't require function definitions:

```json
{
  "tools": [
    {"type": "web_search_preview"},
    {"type": "code_interpreter"},
    {"type": "file_search"}
  ]
}
```

Configure in models.json:

```json
{
  "GPT-4o WebSearch": {
    "model_name": "gpt-4o",
    "api_type": "responses",
    "parameters": {
      "tools": [{"type": "web_search_preview"}]
    }
  }
}
```

## References

- [OpenAI Function Calling Guide](https://platform.openai.com/docs/guides/function-calling)
- [Migrate to the Responses API](https://platform.openai.com/docs/guides/migrate-to-responses)
- [Responses API Reference](https://platform.openai.com/docs/api-reference/responses/create)
