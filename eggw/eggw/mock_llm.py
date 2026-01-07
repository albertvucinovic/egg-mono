"""
Mock LLM client for testing.

Provides canned responses for testing without actual LLM API calls.
Enable with environment variable: EGG_TEST_MODE=true

Supports:
- Simple text responses
- Tool calls (based on message patterns)
- Streaming simulation
"""

import asyncio
import json
import os
import re
from typing import Any, Dict, List, Optional, AsyncIterator


class MockModelRegistry:
    """Mock model registry for testing."""

    def model_options(self, model_key: str) -> Dict[str, Any]:
        return {
            'thinking_content_policy': 'none',
            'supports_tools': True,
        }


class MockLLMClient:
    """Mock LLM client that returns canned responses for testing."""

    def __init__(self, models_path: Optional[str] = None, all_models_path: Optional[str] = None):
        self.registry = MockModelRegistry()
        self._response_delay = float(os.environ.get("EGG_MOCK_DELAY", "0.05"))

    def _get_last_user_message(self, messages: List[Dict[str, Any]]) -> str:
        """Extract the last user message content."""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    return content
                elif isinstance(content, list):
                    # Handle multimodal content
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            return part.get("text", "")
        return ""

    def _should_call_tool(self, user_msg: str, tools: Optional[List[Dict]]) -> Optional[Dict]:
        """Determine if the mock should return a tool call based on message content."""
        if not tools:
            return None

        user_lower = user_msg.lower()

        # Pattern matching for common tool triggers
        tool_patterns = {
            "read": ["read", "show me", "cat ", "view file", "open file"],
            "write": ["write", "create file", "save to"],
            "bash": ["run", "execute", "shell", "$", "command", "ls", "pwd", "mkdir"],
            "glob": ["find files", "search for files", "list files"],
            "grep": ["search for", "find text", "grep"],
            "edit": ["edit", "modify", "change", "update file"],
        }

        for tool_name, patterns in tool_patterns.items():
            for pattern in patterns:
                if pattern in user_lower:
                    # Find matching tool in available tools
                    for tool in tools:
                        func = tool.get("function", {})
                        if func.get("name", "").lower() == tool_name.lower():
                            return self._create_tool_call(tool_name, user_msg)

        return None

    def _create_tool_call(self, tool_name: str, user_msg: str) -> Dict[str, Any]:
        """Create a mock tool call based on the tool name and user message."""
        tool_call_id = f"mock_tc_{os.urandom(4).hex()}"

        # Generate appropriate arguments based on tool type
        if tool_name.lower() == "bash":
            # Extract command from message if present
            cmd = "ls -la"
            if "$" in user_msg:
                # Try to extract command after $
                match = re.search(r'\$\s*(.+?)(?:\s*$|")', user_msg)
                if match:
                    cmd = match.group(1).strip()
            arguments = {"command": cmd}
        elif tool_name.lower() == "read":
            # Try to extract file path
            path = "./test.txt"
            match = re.search(r'(?:read|show|cat|view|open)\s+([^\s]+)', user_msg, re.I)
            if match:
                path = match.group(1)
            arguments = {"file_path": path}
        elif tool_name.lower() == "write":
            arguments = {"file_path": "./test_output.txt", "content": "Test content"}
        elif tool_name.lower() == "glob":
            arguments = {"pattern": "**/*.py"}
        elif tool_name.lower() == "grep":
            arguments = {"pattern": "test", "path": "."}
        elif tool_name.lower() == "edit":
            arguments = {"file_path": "./test.txt", "old_string": "old", "new_string": "new"}
        else:
            arguments = {}

        return {
            "id": tool_call_id,
            "type": "function",
            "function": {
                "name": tool_name,
                "arguments": json.dumps(arguments),
            }
        }

    def _generate_response(self, user_msg: str) -> str:
        """Generate a canned response based on user message."""
        user_lower = user_msg.lower()

        # Pattern-based responses
        if "hello" in user_lower or "hi" in user_lower:
            return "Hello! I'm a mock LLM for testing. How can I help you today?"
        elif "help" in user_lower:
            return "I'm a mock LLM client. I can simulate responses for testing purposes."
        elif "test" in user_lower:
            return "This is a test response from the mock LLM. Everything is working correctly!"
        elif "error" in user_lower:
            return "I detected you mentioned 'error'. In test mode, I can simulate various scenarios."
        elif "explain" in user_lower or "what" in user_lower:
            return "This is a mock explanation. In production, you would get a real AI response here."
        else:
            return f"Mock response to: {user_msg[:100]}{'...' if len(user_msg) > 100 else ''}"

    async def astream_chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = "auto",
        model: Optional[str] = None,
        **kwargs
    ) -> AsyncIterator[List[Dict[str, Any]]]:
        """
        Async streaming chat that yields events in the same format as the real LLM.

        Events yielded:
        - {'type': 'content_delta', 'text': '...'}
        - {'type': 'tool_calls_delta', 'delta': [...]}
        - {'type': 'done', 'message': {...}}
        """
        user_msg = self._get_last_user_message(messages)

        # Check if we should return a tool call
        tool_call = self._should_call_tool(user_msg, tools)

        if tool_call:
            # Yield tool call response
            # First, yield some thinking content
            yield [{"type": "content_delta", "text": "Let me "}]
            await asyncio.sleep(self._response_delay)
            yield [{"type": "content_delta", "text": "help you with that..."}]
            await asyncio.sleep(self._response_delay)

            # Yield tool call delta
            yield [{
                "type": "tool_calls_delta",
                "delta": [{
                    "id": tool_call["id"],
                    "function": {
                        "name": tool_call["function"]["name"],
                        "arguments": tool_call["function"]["arguments"],
                    }
                }]
            }]
            await asyncio.sleep(self._response_delay)

            # Yield done with final message including tool_calls
            yield [{
                "type": "done",
                "message": {
                    "role": "assistant",
                    "content": "Let me help you with that...",
                    "tool_calls": [tool_call],
                }
            }]
        else:
            # Regular text response - stream word by word
            response = self._generate_response(user_msg)
            words = response.split()

            # Stream content in chunks
            chunk_size = 3  # Words per chunk
            for i in range(0, len(words), chunk_size):
                chunk = " ".join(words[i:i + chunk_size])
                if i > 0:
                    chunk = " " + chunk
                yield [{"type": "content_delta", "text": chunk}]
                await asyncio.sleep(self._response_delay)

            # Yield done event
            yield [{
                "type": "done",
                "message": {
                    "role": "assistant",
                    "content": response,
                }
            }]

    def stream_chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = "auto",
        model: Optional[str] = None,
        **kwargs
    ):
        """Sync version - wraps async version."""
        import asyncio

        async def collect():
            results = []
            async for evt in self.astream_chat(messages, tools, tool_choice, model, **kwargs):
                results.append(evt)
            return results

        return asyncio.run(collect())


def is_test_mode() -> bool:
    """Check if test mode is enabled."""
    return os.environ.get("EGG_TEST_MODE", "").lower() in ("true", "1", "yes")


def get_llm_client(models_path: Optional[str] = None, all_models_path: Optional[str] = None):
    """
    Get the appropriate LLM client based on environment.

    Returns MockLLMClient if EGG_TEST_MODE=true, otherwise returns real LLMClient.
    """
    if is_test_mode():
        print("Using MockLLMClient (EGG_TEST_MODE=true)")
        return MockLLMClient(models_path, all_models_path)

    # Try to import real LLMClient
    try:
        from eggllm import LLMClient
        return LLMClient(models_path=models_path or 'models.json',
                        all_models_path=all_models_path or 'all-models.json')
    except Exception as e:
        print(f"Warning: Could not import LLMClient: {e}")
        print("Falling back to MockLLMClient")
        return MockLLMClient(models_path, all_models_path)
