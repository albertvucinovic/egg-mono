from __future__ import annotations

"""Built-in Egg feature plugins."""

from .execution import ExecutionPlugin
from .session import SessionPlugin
from .skills import SkillsPlugin
from .subagents import SubagentsPlugin
from .tools_admin import ToolsAdminPlugin
from .web import WebPlugin

__all__ = ["ExecutionPlugin", "SessionPlugin", "SkillsPlugin", "SubagentsPlugin", "ToolsAdminPlugin", "WebPlugin"]
