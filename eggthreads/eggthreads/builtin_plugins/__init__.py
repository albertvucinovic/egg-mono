from __future__ import annotations

"""Built-in Egg feature plugins."""

from .display_input import DisplayInputPlugin
from .diagnostics import DiagnosticsPlugin
from .execution import ExecutionPlugin
from .auth import AuthPlugin
from .answer_user import AnswerUserPlugin
from .attachments import AttachmentToolsPlugin
from .compaction import CompactionPlugin
from .image_generation import ImageGenerationPlugin
from .inspection import InspectionPlugin
from .long_output import LongOutputPlugin
from .model import ModelPlugin
from .output_optimizer_admin import OutputOptimizerAdminPlugin
from .sandbox_admin import SandboxAdminPlugin
from .session import SessionPlugin
from .skills import SkillsPlugin
from .subagents import SubagentsPlugin
from .thread_ui import ThreadUiPlugin
from .tool_help import ToolHelpPlugin
from .tool_output_extraction import ToolOutputExtractionPlugin
from .tools_admin import ToolsAdminPlugin
from .web import WebPlugin

__all__ = ["AnswerUserPlugin", "ApprovalPoliciesPlugin", "AttachmentToolsPlugin", "AuthPlugin", "CompactionPlugin", "DiagnosticsPlugin", "DisplayInputPlugin", "ExecutionPlugin", "ImageGenerationPlugin", "InspectionPlugin", "LongOutputPlugin", "ModelPlugin", "OutputOptimizerAdminPlugin", "OutputPoliciesPlugin", "SandboxAdminPlugin", "SandboxProvidersPlugin", "SessionPlugin", "SessionProvidersPlugin", "SkillsPlugin", "SubagentsPlugin", "ThreadUiPlugin", "ToolHelpPlugin", "ToolOutputExtractionPlugin", "ToolsAdminPlugin", "WebPlugin"]


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
