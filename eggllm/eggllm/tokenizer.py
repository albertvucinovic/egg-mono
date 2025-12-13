"""Token counting for LLM messages.

Supports accurate token counting for OpenAI models via tiktoken,
with fallback approximate counting for other models.
"""

import json
import warnings
from typing import Dict, List, Any, Optional, Union

try:
    import tiktoken
except ImportError:
    tiktoken = None  # type: ignore


def get_encoding_for_model(model_name: str) -> Optional["tiktoken.Encoding"]:
    """Return tiktoken encoding for a given model name, or None if unknown."""
    if tiktoken is None:
        return None
    try:
        # Try OpenAI's built-in mapping first (supports newer models)
        return tiktoken.encoding_for_model(model_name)
    except KeyError:
        pass
    # Fallback to manual mapping based on model family
    # This may be outdated; users should install tiktoken for accurate counts.
    return None


def approximate_token_count(text: Union[str, Dict, List]) -> int:
    """Approximate token count for arbitrary text/data.

    Uses a simple heuristic: token count ~= characters / 4.
    For structured data, we serialize to JSON and count characters.
    """
    if isinstance(text, dict) or isinstance(text, list):
        # Compact JSON representation, no extra whitespace
        try:
            text = json.dumps(text, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            text = str(text)
    if not isinstance(text, str):
        text = str(text)
    # Rough average of 4 characters per token for English text
    # Add 1 to avoid zero for empty strings
    return max(1, len(text) // 4)


def _model_token_params(model: str):
    """Return (tokens_per_message, tokens_per_name) for a given model.

    Based on OpenAI cookbook; updated for newer models.
    """
    if model in {
        "gpt-3.5-turbo-0613",
        "gpt-3.5-turbo-16k-0613",
        "gpt-4-0314",
        "gpt-4-32k-0314",
        "gpt-4-0613",
        "gpt-4-32k-0613",
    }:
        return (3, 1)
    elif "gpt-3.5-turbo" in model:
        # Assume newer gpt-3.5-turbo models use the same token count as 0613
        return (3, 1)
    elif "gpt-4" in model:
        # Assume newer gpt-4 models use the same token count as 0613
        return (3, 1)
    else:
        # Unknown model, fallback to approximate
        return (0, 0)


def count_tokens_for_message(
    message: Dict[str, Any],
    encoding: Optional["tiktoken.Encoding"] = None,
    model: Optional[str] = None,
) -> int:
    """Count tokens for a single chat message in OpenAI format.

    If encoding is provided, uses accurate tokenization; otherwise falls back
    to approximate counting.
    """
    if encoding is None:
        # Approximate per field
        total = 0
        for key, value in message.items():
            if key == "tool_calls" or key == "tool_call":
                if isinstance(value, list):
                    for tc in value:
                        total += approximate_token_count(tc)
                else:
                    total += approximate_token_count(value)
            else:
                total += approximate_token_count(value)
        return total

    # Accurate counting with encoding
    if model is None:
        # Default to gpt-3.5-turbo-0613 token parameters
        tokens_per_message, tokens_per_name = 3, 1
    else:
        tokens_per_message, tokens_per_name = _model_token_params(model)

    num_tokens = tokens_per_message
    for key, value in message.items():
        if key == "name":
            num_tokens += tokens_per_name
            # Name value is counted as part of the content? Actually the name field
            # replaces the role token? We'll follow cookbook: add tokens_per_name and encode name.
            if isinstance(value, str):
                num_tokens += len(encoding.encode(value))
            continue
        if key == "tool_calls":
            if isinstance(value, list):
                for tc in value:
                    # Serialize tool call as JSON string
                    tc_str = json.dumps(tc, ensure_ascii=False, separators=(",", ":"))
                    num_tokens += len(encoding.encode(tc_str))
            continue
        if isinstance(value, str):
            num_tokens += len(encoding.encode(value))
        elif isinstance(value, (int, float, bool)):
            num_tokens += len(encoding.encode(str(value)))
        elif isinstance(value, (dict, list)):
            # Should not happen for standard OpenAI messages, but fallback
            tc_str = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            num_tokens += len(encoding.encode(tc_str))
        # ignore other types
    return num_tokens


def num_tokens_from_messages(messages: List[Dict[str, Any]], model: str) -> int:
    """Return the number of tokens used by a list of messages.

    Exact for known OpenAI models, approximate otherwise.
    """
    encoding = get_encoding_for_model(model)
    if encoding is None:
        # Approximate total
        total = 0
        for msg in messages:
            total += approximate_token_count(msg)
        return total

    tokens_per_message, tokens_per_name = _model_token_params(model)
    num_tokens = 0
    for message in messages:
        num_tokens += tokens_per_message
        for key, value in message.items():
            if key == "name":
                num_tokens += tokens_per_name
                if isinstance(value, str):
                    num_tokens += len(encoding.encode(value))
                continue
            if key == "tool_calls":
                if isinstance(value, list):
                    for tc in value:
                        tc_str = json.dumps(tc, ensure_ascii=False, separators=(",", ":"))
                        num_tokens += len(encoding.encode(tc_str))
                continue
            if isinstance(value, str):
                num_tokens += len(encoding.encode(value))
            elif isinstance(value, (int, float, bool)):
                num_tokens += len(encoding.encode(str(value)))
            elif isinstance(value, (dict, list)):
                tc_str = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                num_tokens += len(encoding.encode(tc_str))
    # Every reply is primed with <|start|>assistant<|message|>
    num_tokens += 3
    return num_tokens


def count_tokens_for_messages(
    messages: List[Dict[str, Any]],
    model: str,
    tools: Optional[List[Dict[str, Any]]] = None,
) -> int:
    """Total tokens for a chat completion request.

    Includes tokens from messages and optionally tool definitions.
    """
    token_count = num_tokens_from_messages(messages, model)
    if tools:
        # Approximate tool definitions token count
        token_count += approximate_token_count(tools)
    return token_count


# Export convenience functions
count_tokens = count_tokens_for_messages
