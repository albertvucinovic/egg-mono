from __future__ import annotations

"""Anthropic Messages API adapter."""

import json
from typing import Any, Dict, Optional

import requests

from .base import ProviderAdapter, attach_provider_usage, requests_timeout_arg


class AnthropicMessagesAdapter(ProviderAdapter):
    """Streams Anthropic Messages SSE responses.

    Egg's runner already shapes attachment content into Anthropic content
    blocks.  This adapter only translates the OpenAI-ish chat payload envelope
    into Anthropic's Messages request envelope and normalizes the stream back
    to Egg provider events.
    """

    def _build_payload(self, original_payload: Dict[str, Any]) -> Dict[str, Any]:
        system_parts: list[str] = []
        messages: list[Dict[str, Any]] = []
        for message in original_payload.get("messages") or []:
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            content = message.get("content", "")
            if role == "system":
                if isinstance(content, str):
                    system_parts.append(content)
                elif isinstance(content, list):
                    text = "\n".join(str(part.get("text") or "") for part in content if isinstance(part, dict) and part.get("type") == "text")
                    if text:
                        system_parts.append(text)
                continue
            if role not in {"user", "assistant"}:
                continue
            messages.append({"role": role, "content": content})

        out: Dict[str, Any] = {
            "model": original_payload.get("model"),
            "messages": messages,
            "stream": True,
            "max_tokens": int(original_payload.get("max_tokens") or original_payload.get("max_output_tokens") or 4096),
        }
        if system_parts:
            out["system"] = "\n\n".join(part for part in system_parts if part)
        for key in ("temperature", "top_p", "top_k", "stop_sequences", "metadata"):
            if key in original_payload:
                out[key] = original_payload[key]
        return out

    @staticmethod
    def _merge_usage(current: Optional[Dict[str, Any]], update: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(update, dict):
            return current
        out = dict(current or {})
        out.update(update)
        return out

    def stream(
        self,
        url: str,
        headers: Dict[str, str],
        payload: Dict[str, Any],
        timeout: int = 600,
        session: Optional[requests.Session] = None,
    ):
        sess = session or requests
        resp = sess.post(
            url,
            headers=headers,
            json=self._build_payload(payload),
            timeout=requests_timeout_arg(timeout),
            stream=True,
        )
        resp.raise_for_status()

        assistant_text_parts: list[str] = []
        provider_usage: Optional[Dict[str, Any]] = None

        for line in resp.iter_lines():
            if not line:
                continue
            line_str = line.decode("utf-8", errors="ignore")
            if not line_str.startswith("data: "):
                continue
            data_str = line_str[6:]
            if data_str == "[DONE]":
                break
            try:
                event = json.loads(data_str)
            except Exception:
                continue
            if not isinstance(event, dict):
                continue
            usage = event.get("usage")
            if isinstance(usage, dict):
                provider_usage = self._merge_usage(provider_usage, usage)
            delta = event.get("delta")
            if isinstance(delta, dict):
                if isinstance(delta.get("usage"), dict):
                    provider_usage = self._merge_usage(provider_usage, delta["usage"])
                text = delta.get("text")
                if isinstance(text, str) and text:
                    assistant_text_parts.append(text)
                    yield {"type": "content_delta", "text": text}
            message = event.get("message")
            if isinstance(message, dict) and isinstance(message.get("usage"), dict):
                provider_usage = self._merge_usage(provider_usage, message["usage"])

        final_message: Dict[str, Any] = {"role": "assistant"}
        content = "".join(assistant_text_parts)
        if content:
            final_message["content"] = content
        attach_provider_usage(final_message, provider_usage)
        yield {"type": "done", "message": final_message}


__all__ = ["AnthropicMessagesAdapter"]