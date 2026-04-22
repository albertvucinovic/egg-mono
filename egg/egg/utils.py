"""Utility functions and constants for the egg application."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Root directory of the egg application
ROOT = Path(__file__).resolve().parent

# Path constants - eggconfig provides canonical locations for model config files
from eggconfig import get_models_path, get_all_models_path

MODELS_PATH = get_models_path()
ALL_MODELS_PATH = get_all_models_path()
SYSTEM_PROMPT_PATH = ROOT / 'systemPrompt'

COMMANDS_TEXT = """
Commands:
  Model handling:
    /model <key>, /updateAllModels <provider>
  Thread management basic:
    /spawnChildThread <text>, /spawnAutoApprovedChildThread <text>, /waitForThreads <threads>
  Thread management other:
    /parentThread, /listChildren, /threads, /thread <selector>
    /deleteThread <selector>, /newThread <name>, /duplicateThread <name>
    /schedulers
  Tool management:
    /toggleAutoApproval, /toolsOn, /toolsOff, /disableTool <name>, /enableTool <name>
    /toggleSandboxing, /setSandboxConfiguration <file.json>
    /getSandboxingConfig
    /toolsSecrets <on|off>, /toolsStatus
  Display:
    /togglePanel (chat|children|system)
    /toggleBorders
    /redraw
    /displayMode (full-screen|inline) — full-screen uses alt-screen with
      in-app scrolling + streaming-as-static; inline uses the terminal's
      native scrollback (HEAD behavior, smallest diff, shell-integrated).
  Auth (ChatGPT OAuth):
    /login, /logout, /authStatus
  Other:
    /enterMode <send|newline>, /cost, /paste, /quit
    /setContextLimit [limit]
    /help
"""


def get_system_prompt() -> str:
    """Load the system prompt from the systemPrompt file."""
    try:
        with open(SYSTEM_PROMPT_PATH, 'r', encoding='utf-8') as f:
            return f.read().strip()
    except Exception:
        return "You are a helpful assistant."


def snapshot_messages(db, thread_id: str) -> List[Dict[str, Any]]:
    """Extract messages from a thread's snapshot."""
    th = db.get_thread(thread_id)
    if not th or not th.snapshot_json:
        return []
    try:
        snap = json.loads(th.snapshot_json)
        msgs = snap.get('messages', [])
        return msgs
    except Exception:
        return []


def get_subtree(db, root_id: str) -> List[str]:
    """Return all thread IDs in a subtree (excluding the root itself)."""
    from eggthreads import list_children_ids
    out: List[str] = []
    q = [root_id]
    seen = set()
    while q:
        t = q.pop(0)
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
        try:
            for cid in list_children_ids(db, t):
                q.append(cid)
        except Exception:
            pass
    return out[1:]


def looks_markdown(content: str) -> bool:
    """Heuristic check if content looks like markdown."""
    if not content:
        return False
    indicators = ['```', '# ', '## ', '### ', '* ', '- ', '> ', '`']
    hits = sum(1 for i in indicators if i in content)
    if hits >= 2:
        return True
    if content.count('\n') >= 2 and hits >= 1:
        return True
    return False


def shorten_output_preview(text: str, max_lines: int = 200, max_chars: int = 8000) -> str:
    """Return a shortened preview for very long tool outputs.

    This keeps at most max_lines and max_chars of content and appends
    an ellipsis notice when truncation occurs.
    """
    if not isinstance(text, str) or not text:
        return ""
    lines = text.splitlines()
    truncated = text
    if len(lines) > max_lines:
        truncated = "\n".join(lines[:max_lines])
    if len(truncated) > max_chars:
        truncated = truncated[:max_chars]
    if truncated != text:
        truncated = truncated.rstrip()
        truncated += "\n\n...[output truncated for preview]..."
    return truncated


def read_clipboard() -> Optional[str]:
    """Return clipboard content as string, or None on failure."""
    # Try pyperclip first
    try:
        import pyperclip
        return pyperclip.paste()
    except ImportError:
        pass

    # Fallback to platform-specific commands with timeout
    platform = sys.platform

    def run_clipboard_cmd(cmd, **kwargs):
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=2, **kwargs)
            if result.returncode == 0:
                return result.stdout
            else:
                return None
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError,
                FileNotFoundError, UnicodeDecodeError):
            return None
        except Exception:
            return None

    if platform == "darwin":  # macOS
        out = run_clipboard_cmd(["pbpaste"])
        if out is not None:
            return out
    elif platform == "win32":  # Windows
        out = run_clipboard_cmd(["clip"], shell=True)
        if out is not None:
            return out
    else:  # Linux/BSD
        # Try wl-paste (Wayland) first, then xclip, then xsel
        out = run_clipboard_cmd(["wl-paste"])
        if out is not None:
            return out
        out = run_clipboard_cmd(["xclip", "-selection", "clipboard", "-o"])
        if out is not None:
            return out
        out = run_clipboard_cmd(["xsel", "--clipboard", "--output"])
        if out is not None:
            return out
    return None


def restore_tty() -> None:
    """Best-effort restoration of terminal settings (echo / canonical).

    In some environments, libraries like readchar or low-level input
    handling may leave the TTY with echo or canonical mode disabled
    if the process exits unexpectedly. This function tries to ensure
    that, when Egg exits, the terminal is in a sane state so that
    subsequent shell input is visible again.
    """
    try:
        import termios
    except Exception:
        return
    try:
        if not sys.stdin.isatty():
            return
        fd = sys.stdin.fileno()
        try:
            attrs = termios.tcgetattr(fd)
        except Exception:
            return
        # Ensure echo and canonical mode are enabled
        lflag = attrs[3]
        lflag |= termios.ECHO | termios.ICANON
        attrs[3] = lflag
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, attrs)
        except Exception:
            pass
    except Exception:
        pass
