from __future__ import annotations

import json
from typing import Any, Callable, Dict, List


class ToolRegistry:
    """Simple registry for OpenAI function-call compatible tools.

    - tools_spec() returns the JSON schema list to pass to the LLM
    - execute(name, arguments) dispatches to the registered callable
    """

    def __init__(self):
        self._tools: Dict[str, Dict[str, Any]] = {}

    def register(self, name: str, description: str, parameters_schema: Dict[str, Any], impl: Callable[[Dict[str, Any]], Any]):
        self._tools[name] = {
            "spec": {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": parameters_schema,
                }
            },
            "impl": impl,
        }

    def tools_spec(self) -> List[Dict[str, Any]]:
        return [d["spec"] for d in self._tools.values()]

    def execute(self, name: str, arguments: Any) -> Any:
        entry = self._tools.get(name)
        if not entry:
            raise KeyError(f"Unknown tool: {name}")
        impl = entry["impl"]
        if isinstance(arguments, str):
            try:
                args = json.loads(arguments) if arguments.strip() else {}
            except Exception:
                args = {"_raw": arguments}
        elif isinstance(arguments, dict):
            args = arguments
        else:
            args = {"_arg": arguments}
        return impl(args)


# Default tools similar to chat.sh
def create_default_tools() -> ToolRegistry:
    import asyncio, subprocess, sys, os, json as _json
    from io import StringIO
    from pathlib import Path

    reg = ToolRegistry()

    # bash
    def _bash(args: Dict[str, Any]):
        script = args.get('script', '')
        res = subprocess.run(script, shell=True, executable='/bin/bash', capture_output=True, text=True)
        out = ''
        if res.stdout:
            out += f"--- STDOUT ---\n{res.stdout.strip()}\n"
        if res.stderr:
            out += f"--- STDERR ---\n{res.stderr.strip()}\n"
        return out.strip() or "--- The command executed successfully and produced no output ---"

    reg.register(
        name='bash',
        description='Execute a bash script and return combined stdout/stderr.',
        parameters_schema={
            "type": "object",
            "properties": {"script": {"type": "string"}},
            "required": ["script"],
        },
        impl=_bash,
    )

    # python
    def _python(args: Dict[str, Any]):
        script = args.get('script', '')
        old_stdout, old_stderr = sys.stdout, sys.stderr
        redirected_stdout = sys.stdout = StringIO()
        redirected_stderr = sys.stderr = StringIO()
        try:
            exec(script, {})
        except Exception as e:
            print(f"Error executing Python script: {e}")
        finally:
            sys.stdout, sys.stderr = old_stdout, old_stderr
        out = ''
        if redirected_stdout.getvalue().strip():
            out += f"--- STDOUT ---\n{redirected_stdout.getvalue().strip()}\n"
        if redirected_stderr.getvalue().strip():
            out += f"--- STDERR ---\n{redirected_stderr.getvalue().strip()}\n"
        return out.strip() or "--- The script executed successfully and produced no output ---"

    reg.register(
        name='python',
        description='Execute a Python script and return combined stdout/stderr.',
        parameters_schema={
            "type": "object",
            "properties": {"script": {"type": "string"}},
            "required": ["script"],
        },
        impl=_python,
    )

    # javascript (placeholder for remote-debugging execution; here we only echo input)
    def _javascript(args: Dict[str, Any]):
        script = args.get('script', '')
        url = args.get('url', '')
        return _json.dumps({"script": script[:200], "url": url})

    reg.register(
        name='javascript',
        description='Execute JavaScript in a browser via remote debugging (app layer should implement).',
        parameters_schema={
            "type": "object",
            "properties": {"script": {"type": "string"}, "url": {"type": "string"}},
            "required": ["script"],
        },
        impl=_javascript,
    )

    # popContext (stub)
    def _popContext(args: Dict[str, Any]):
        # In chat.sh this signals returning a value to a parent agent. Here we just echo back.
        return _json.dumps({"popContext": True, "args": args})

    reg.register(
        name='popContext',
        description='Stub: return a result to the spawning agent (no-op in this app).',
        parameters_schema={
            "type": "object",
            "properties": {"return_value": {"type": "string"}},
            "required": ["return_value"],
        },
        impl=_popContext,
    )

    # spawn_agent (stub)
    def _spawn_agent(args: Dict[str, Any]):
        # The app can implement real agent spawning. Here we just acknowledge the request.
        label = args.get('label') or 'agent'
        ctx = args.get('context_text') or ''
        return f"spawn_agent stub: label={label!r}, context_len={len(ctx)}"

    reg.register(
        name='spawn_agent',
        description='Stub: spawn a child agent (no-op; app may override).',
        parameters_schema={
            "type": "object",
            "properties": {
                "context_text": {"type": "string"},
                "label": {"type": "string"}
            },
            "required": ["context_text"],
        },
        impl=_spawn_agent,
    )

    # spawn_agent_auto (stub)
    def _spawn_agent_auto(args: Dict[str, Any]):
        label = args.get('label') or 'agent'
        ctx = args.get('context_text') or ''
        return f"spawn_agent_auto stub: label={label!r}, context_len={len(ctx)}"

    reg.register(
        name='spawn_agent_auto',
        description='Stub: spawn a child agent with auto-approval (no-op; app may override).',
        parameters_schema={
            "type": "object",
            "properties": {
                "context_text": {"type": "string"},
                "label": {"type": "string"}
            },
            "required": ["context_text"],
        },
        impl=_spawn_agent_auto,
    )

    # replace_between
    def _replace_between(args: Dict[str, Any]):
        file_path = args.get('file_path', '')
        start_text = args.get('start_text', '')
        end_text = args.get('end_text', '')
        new_text = args.get('new_text', '')
        p = Path(file_path)
        content = p.read_text() if p.exists() else ''
        sidx = content.find(start_text)
        if sidx == -1:
            return "Error: start_text not found."
        eidx = content.find(end_text, sidx + len(start_text))
        if eidx == -1:
            return "Error: end_text not found after start_text."
        new_content = content[:sidx] + new_text + content[eidx + len(end_text):]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(new_content)
        return "Success: replaced region."

    reg.register(
        name='replace_between',
        description='Replace the first region between two exact string boundaries (start_text and the first subsequent end_text) with new_text. Exact literal matching only (no regex). The boundaries themselves are also replaced. Works across line breaks.',
        parameters_schema={
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "start_text": {"type": "string"},
                "end_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            "required": ["file_path", "start_text", "end_text", "new_text"],
        },
        impl=_replace_between,
    )

    # search_tavily (simple wrapper; requires TAVILY_API_KEY env var)
    def _search_tavily(args: Dict[str, Any]):
        import os as _os, requests as _requests
        query = args.get('query', '')
        api_key = _os.environ.get('TAVILY_API_KEY')
        if not api_key:
            return 'Error: TAVILY_API_KEY not set in environment.'
        try:
            resp = _requests.post(
                'https://api.tavily.com/search',
                json={'query': query, 'max_results': 5, 'include_answer': False, 'search_depth': 'basic'},
                headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {api_key}'},
                timeout=20,
            )
            if resp.status_code != 200:
                return f"Error: Tavily API status {resp.status_code}: {resp.text[:400]}"
            data = resp.json()
            results = data.get('results') or data.get('data') or []
            lines = []
            for r in results[:5]:
                title = (r.get('title') or '').strip()
                url = (r.get('url') or r.get('link') or '').strip()
                if title or url:
                    lines.append(f"- {title}  {url}")
            return "\n".join(lines) or "No results."
        except Exception as e:
            return f"Error: Tavily request failed: {e}"

    reg.register(
        name='search_tavily',
        description='Perform a web search (using Tavily) and return up to 5 results with titles and URLs.',
        parameters_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        impl=_search_tavily,
    )

    # Note: agent-oriented tools like popContext/spawn_agent are excluded from default registry
    # to prevent unintended tool calls in basic chats. The UI layer can register them explicitly.

    return reg

