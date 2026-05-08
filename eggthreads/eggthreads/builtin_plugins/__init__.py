from __future__ import annotations

"""Built-in Egg feature plugins."""

from .execution import ExecutionPlugin
from .sandbox_admin import SandboxAdminPlugin
from .session import SessionPlugin
from .skills import SkillsPlugin
from .subagents import SubagentsPlugin
from .thread_ui import ThreadUiPlugin
from .tools_admin import ToolsAdminPlugin
from .web import WebPlugin

__all__ = ["ExecutionPlugin", "SandboxAdminPlugin", "SessionPlugin", "SkillsPlugin", "SubagentsPlugin", "ThreadUiPlugin", "ToolsAdminPlugin", "WebPlugin"]
