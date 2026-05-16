from __future__ import annotations

"""Built-in Egg feature plugins."""

from .display_input import DisplayInputPlugin
from .diagnostics import DiagnosticsPlugin
from .execution import ExecutionPlugin
from .auth import AuthPlugin
from .answer_user import AnswerUserPlugin
from .compaction import CompactionPlugin
from .model import ModelPlugin
from .sandbox_admin import SandboxAdminPlugin
from .session import SessionPlugin
from .skills import SkillsPlugin
from .subagents import SubagentsPlugin
from .thread_ui import ThreadUiPlugin
from .tools_admin import ToolsAdminPlugin
from .web import WebPlugin

__all__ = ["AnswerUserPlugin", "ApprovalPoliciesPlugin", "AuthPlugin", "CompactionPlugin", "DiagnosticsPlugin", "DisplayInputPlugin", "ExecutionPlugin", "ModelPlugin", "OutputPoliciesPlugin", "SandboxAdminPlugin", "SandboxProvidersPlugin", "SessionPlugin", "SessionProvidersPlugin", "SkillsPlugin", "SubagentsPlugin", "ThreadUiPlugin", "ToolsAdminPlugin", "WebPlugin"]


def __getattr__(name: str):
    if name == "ApprovalPoliciesPlugin":
        from .approval_policies import ApprovalPoliciesPlugin

        return ApprovalPoliciesPlugin
    if name == "SandboxProvidersPlugin":
        from .sandbox_providers import SandboxProvidersPlugin

        return SandboxProvidersPlugin
    if name == "SessionProvidersPlugin":
        from .session_providers import SessionProvidersPlugin

        return SessionProvidersPlugin
    if name == "OutputPoliciesPlugin":
        from .output_policies import OutputPoliciesPlugin

        return OutputPoliciesPlugin
    raise AttributeError(name)
