from __future__ import annotations

import pytest

from eggthreads.tool_effects import ToolEffect, classify_bash_script, classify_tool_effect


@pytest.mark.parametrize(
    ("script", "effect"),
    [
        ("nl -ba a.py | sed -n '1,80p'; rg -n 'needle' src | head -20", ToolEffect.READ),
        ("git status --short && git diff --stat", ToolEffect.READ),
        ("git add a.py && git commit -m test", ToolEffect.MAY_WRITE),
        ("find . -name '*.py' -delete", ToolEffect.MAY_WRITE),
        ("find . -name '*.py' -print", ToolEffect.READ),
        ("sed -i 's/a/b/' a.py", ToolEffect.MAY_WRITE),
        ("echo hello > output.txt", ToolEffect.MAY_WRITE),
        ("echo hello 2>error.log", ToolEffect.MAY_WRITE),
        ("rg needle 2>&1 | head", ToolEffect.READ),
        ("custom-command --inspect", ToolEffect.UNKNOWN),
        ("python -c 'print(1)'", ToolEffect.UNKNOWN),
        ("applypatch <<'PATCH'\n*** Begin Patch\nPATCH", ToolEffect.MAY_WRITE),
    ],
)
def test_classify_bash_script_is_conservative(script, effect):
    assert classify_bash_script(script).effect is effect


@pytest.mark.parametrize(
    ("name", "arguments", "effect"),
    [
        ("read_long_tool_output", {}, ToolEffect.READ),
        ("web_search", {"query": "egg"}, ToolEffect.READ),
        ("save_provider_artifact_to_file", {"artifact_id": "x"}, ToolEffect.MAY_WRITE),
        ("python_repl", {"code": "print(1)"}, ToolEffect.UNKNOWN),
        ("other_plugin", {}, ToolEffect.UNKNOWN),
        ("bash", '{"script":"git log -1"}', ToolEffect.READ),
    ],
)
def test_classify_tool_effect(name, arguments, effect):
    assert classify_tool_effect(name, arguments).effect is effect
