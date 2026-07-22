from __future__ import annotations

"""Built-in skills plugin.

The shared service here is intentionally small: both the `skill` tool and the
future `/skills` + `/skill` commands should call the same renderer so behavior
stays aligned.
"""

from dataclasses import dataclass
from typing import Any, Dict

from ..plugins import PluginContext
from ..tool_output_presentation import line_number_presentation
from ..tools import ToolExecutionResult, ToolRegistry


def render_skill_request(args: Dict[str, Any]) -> str | ToolExecutionResult:
    """Render the requested skill listing/search/document."""

    from ..skills import render_skill_tool_output

    name = args.get("name")
    if name is None:
        # Accept a raw positional argument for local/tool bridge callers.
        name = args.get("_arg")
    query = args.get("query")
    output = render_skill_tool_output(
        str(name) if name is not None else None,
        query=str(query) if query is not None else None,
    )
    if args.get("line_numbers") is True:
        return ToolExecutionResult(
            output,
            publication_presentation=line_number_presentation(),
        )
    return output


def _skill_rendered_text(value: str | ToolExecutionResult) -> str:
    """Return command/UI text even if the shared renderer adds presentation."""

    return value.presented_output() if isinstance(value, ToolExecutionResult) else value


def _log(context: Any, message: str) -> None:
    if context.log_system is not None:
        context.log_system(message)


def skills_command(context: Any, arg: str):
    from ..command_catalog import CommandResult

    try:
        query = (arg or "").strip()
        text = _skill_rendered_text(
            render_skill_request({"query": query} if query else {})
        )
        _log(context, "Skills list (see console for full).")
        if context.console_print_block is not None:
            context.console_print_block("Skills", text, border_style="cyan")
        else:
            _log(context, text)
    except Exception as e:
        _log(context, f"/skills error: {e}")
        return CommandResult(clear_input=False)
    return CommandResult(clear_input=True)


def skill_command(context: Any, arg: str):
    from ..command_catalog import CommandResult
    from ..skills import get_skill

    name = (arg or "").strip()
    if not name:
        _log(context, "Usage: /skill <name>")
        return CommandResult(clear_input=False)
    try:
        skill = get_skill(name)
        text = _skill_rendered_text(
            render_skill_request({"name": skill.name})
        )
        marker = f"<!-- egg-skill:{skill.name} -->"
        context_text = f"{marker}\n{text}"
        loaded = False
        already_loaded = False
        db = context.db if context.db is not None else getattr(context.app, "db", None)
        thread_id = context.current_thread or getattr(context.app, "current_thread", None)
        if db is not None and thread_id:
            try:
                from egg.utils import snapshot_messages  # type: ignore
            except Exception:
                snapshot_messages = None
            messages = snapshot_messages(db, thread_id) if snapshot_messages is not None else []
            already_loaded = any(
                marker in str(message.get("content") or "")
                for message in messages
                if message.get("role") == "system"
            )
            if not already_loaded:
                import eggthreads as _eggthreads

                _eggthreads.append_message(db, thread_id, "system", context_text)
                _eggthreads.create_snapshot(db, thread_id)
                loaded = True
        if loaded:
            _log(context, f"Skill /{skill.name} loaded into thread context (see console for full).")
        elif already_loaded:
            _log(context, f"Skill /{skill.name} already loaded; showing document.")
        else:
            _log(context, f"Skill /{skill.name} (see console for full).")
        if context.console_print_block is not None:
            context.console_print_block(f"Skill: {skill.title}", text, border_style="cyan")
        else:
            _log(context, text)
    except KeyError:
        _log(context, f"Unknown skill: {name}")
        return CommandResult(clear_input=False)
    except Exception as e:
        _log(context, f"/skill error: {e}")
        return CommandResult(clear_input=False)
    return CommandResult(clear_input=True)


def skill_name_completions(context: Any, arg: str):
    from ..skills import list_skills

    prefix = (arg or "").strip()
    return [skill.name for skill in list_skills() if not prefix or skill.name.startswith(prefix)]


def register_skill_commands(registry: Any) -> None:
    from ..command_catalog import CommandSpec

    registry.register(CommandSpec("skills", skills_command, category="skills", usage="/skills [query]", description="List or search packaged skills."))
    registry.register(CommandSpec("skill", skill_command, category="skills", usage="/skill <name>", description="Show and load a packaged skill.", complete=skill_name_completions))


def register_skill_tool(registry: ToolRegistry) -> None:
    registry.register(
        name="skill",
        description=(
            "List available Egg skill documents, search skills, or load one skill by name. "
            "Skills are markdown instructions/examples/snippets; this tool is read-only "
            "and does not install new runtime APIs."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Optional skill name to load, for example 'rlm'. Omit to list skills.",
                },
                "query": {
                    "type": "string",
                    "description": "Optional plain substring search over skill names, descriptions, and documents.",
                },
                "line_numbers": {
                    "type": "boolean",
                    "default": False,
                    "description": "Prefix canonical output lines with 1-based line numbers for presentation only.",
                },
            },
        },
        impl=render_skill_request,
        capabilities={"supports_cross_thread_execution": True},
    )


@dataclass(frozen=True)
class SkillsPlugin:
    name: str = "skills"
    version: str = "0"

    def register(self, context: PluginContext) -> None:
        if context.tool_registry is not None:
            register_skill_tool(context.tool_registry)
        if context.command_registry is not None:
            register_skill_commands(context.command_registry)


__all__ = [
    "SkillsPlugin",
    "register_skill_commands",
    "register_skill_tool",
    "render_skill_request",
    "skill_command",
    "skill_name_completions",
    "skills_command",
]
