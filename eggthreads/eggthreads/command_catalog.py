from __future__ import annotations

"""Shared UI command/autocomplete catalog for Egg frontends."""

from typing import List


SESSION_COMMAND_COMPLETIONS: List[str] = [
    '/sessionStatus',
    '/sessionOn',
    '/sessionOff',
    '/sessionStop',
    '/sessionReset',
    '/sessionCleanup',
    '/pythonRepl',
    '/bashRepl',
]

SESSION_ON_COMPLETIONS: List[str] = [
    'provider=docker',
    'provider=memory',
    'image=egg-rlm-session',
    'share_with_children=true',
    'share_with_children=false',
    'share_repl=true',
    'share_repl=false',
]

SESSION_TARGET_COMPLETIONS: List[str] = ['python', 'bash', 'all']

EGG_COMMAND_COMPLETIONS: List[str] = [
    '/help', '/model', '/updateAllModels',
    '/spawnChildThread', '/spawnAutoApprovedChildThread', '/waitForThreads', '/parentThread',
    '/listChildren', '/threads', '/thread', '/deleteThread', '/newThread', '/duplicateThread',
    '/continue', '/rename',
    '/skills', '/skill',
    '/schedulers', '/enterMode', '/toggleAutoApproval',
    '/toolsOn', '/toolsOff', '/disableTool', '/enableTool', '/toolsStatus', '/toolInfo',
    '/toolsSecrets', '/toggleSandboxing', '/quit', '/paste',
    '/setSandboxConfiguration', '/getSandboxingConfig',
    *SESSION_COMMAND_COMPLETIONS,
    '/setContextLimit', '/setThreadPriority',
    '/togglePanel', '/toggleBorders', '/redraw', '/displayMode',
    '/login', '/logout', '/authStatus',
    '/startSearxng', '/stopSearxng',
]

EGGW_COMMAND_COMPLETIONS: List[str] = [
    *EGG_COMMAND_COMPLETIONS,
    # Web-only aliases/options.
    '/spawn',
    '/theme',
]


__all__ = [
    'SESSION_COMMAND_COMPLETIONS',
    'SESSION_ON_COMPLETIONS',
    'SESSION_TARGET_COMPLETIONS',
    'EGG_COMMAND_COMPLETIONS',
    'EGGW_COMMAND_COMPLETIONS',
]
