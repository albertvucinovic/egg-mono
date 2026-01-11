"""Autocomplete logic for eggw backend."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter

from eggthreads import list_threads

import core

# Available themes (text-colored variants first, then background variants)
THEMES = [
    # Text-colored themes (uniform background, colored text)
    "dark", "cyberpunk", "forest", "ocean", "sunset", "mono", "midnight",
    "disney", "fruit", "vegetables", "coffee", "matrix", "light", "light-mono",
    "colorful", "colorful-light",
    # Background variants (colored backgrounds)
    "dark-background", "cyberpunk-background", "forest-background", "ocean-background",
    "sunset-background", "mono-background", "midnight-background", "disney-background",
    "fruit-background", "vegetables-background", "coffee-background", "matrix-background",
    "light-background", "light-mono-background", "colorful-light-background",
]

router = APIRouter(tags=["autocomplete"])


def last_token(s: str) -> str:
    """Get last token for partial matching."""
    m = re.search(r"([\w\-.:/~]+)$", s)
    return m.group(1) if m else ""


@router.get("/api/autocomplete")
async def get_autocomplete(
    line: str,
    cursor: int = -1,
    thread_id: Optional[str] = None,
):
    """Get autocomplete suggestions for the input line.

    Returns a list of suggestions with:
    - display: text to show in dropdown
    - insert: text to insert at cursor
    - replace: number of chars to delete before inserting (optional)
    - meta: additional info to show (optional)
    """
    if not core.db:
        return {"suggestions": []}

    if cursor < 0:
        cursor = len(line)

    prefix = line[:cursor]
    suggestions = []

    # Command completion
    if prefix.startswith('/'):
        sp = prefix.find(' ')
        if sp == -1:
            # Complete command name - always return full command for robust replacement
            commands = [
                '/help', '/model', '/updateAllModels',
                '/spawn', '/spawnAutoApprovedChildThread', '/newThread',
                '/threads', '/thread', '/parentThread', '/listChildren',
                '/deleteThread', '/duplicateThread', '/rename', '/waitForThreads', '/continue',
                '/toggleAutoApproval', '/toolsOn', '/toolsOff', '/toolsStatus',
                '/disableTool', '/enableTool', '/toolsSecrets',
                '/toggleSandboxing', '/setSandboxConfiguration', '/getSandboxingConfig',
                '/togglePanel', '/toggleBorders', '/theme',
                '/cost', '/schedulers', '/enterMode', '/paste', '/quit',
            ]
            pref_lower = prefix.lower()
            for cmd in commands:
                if pref_lower in cmd.lower():
                    suggestions.append({
                        "display": cmd,
                        "insert": cmd,  # Full command for replacement
                        "replace": len(prefix),
                    })
        else:
            # Complete command arguments
            cmd = prefix[:sp]
            arg = prefix[sp+1:]
            arg_tok = last_token(arg)

            if cmd == '/model':
                # Model name suggestions - replace entire argument (supports multi-word search)
                # Strip trailing whitespace from arg for matching
                arg_stripped = arg.rstrip()
                if arg_stripped:
                    # Split into words and check if all words are found in the model name
                    words = arg_stripped.lower().split()
                    for key in sorted(core.models_config.keys()):
                        if all(w in key.lower() for w in words):
                            suggestions.append({
                                "display": key,
                                "insert": key,
                                "replace": len(arg_stripped),  # Replace entire argument
                            })
                else:
                    # No argument - show all models
                    for key in sorted(core.models_config.keys()):
                        suggestions.append({
                            "display": key,
                            "insert": key,
                            "replace": 0,
                        })

            elif cmd in ('/thread', '/deleteThread', '/waitForThreads'):
                # Thread ID suggestions with rich info like egg.py
                arg_lower = arg_tok.lower()
                threads = list_threads(core.db)
                # Sort by created_at descending
                try:
                    threads.sort(key=lambda t: t.created_at or '', reverse=True)
                except:
                    pass

                # Get current thread ID for [CUR] indicator
                cur_thread_id = thread_id

                # Check which threads are streaming (have active schedulers)
                streaming_threads = set(core.active_schedulers.keys())

                # Filter ALL threads first, then limit results
                matched_count = 0
                for t in threads:
                    tid = t.thread_id
                    name = t.name or ''
                    recap = t.short_recap or ''
                    status = t.status or 'unknown'
                    hay = f"{tid} {name} {recap}".lower()
                    if arg_lower and arg_lower not in hay:
                        continue

                    # Build display like egg.py
                    parts = []
                    if tid == cur_thread_id:
                        parts.append("[CUR]")
                    if tid in streaming_threads:
                        parts.append("[STREAM]")
                    parts.append(tid[-8:])

                    # Status indicator
                    if status == 'active':
                        parts.append(f"<{status}>")
                    elif status not in ('waiting_user', 'unknown'):
                        parts.append(f"<{status}>")

                    if recap:
                        parts.append(f"- {recap[:30]}")
                    if name:
                        parts.append(f"({name})")

                    display = " ".join(parts)
                    suggestions.append({
                        "display": display,
                        "insert": tid,
                        "replace": len(arg_tok),
                    })
                    matched_count += 1
                    if matched_count >= 50:  # Limit results after filtering
                        break

            elif cmd == '/setSandboxConfiguration':
                # Suggest sandbox config files from .egg/sandbox/
                sandbox_dir = Path.cwd() / ".egg" / "sandbox"
                if sandbox_dir.is_dir():
                    try:
                        arg_lower = arg_tok.lower()
                        for f in sorted(sandbox_dir.iterdir()):
                            if f.is_file() and f.suffix == '.json':
                                name = f.name
                                if not arg_lower or arg_lower in name.lower():
                                    suggestions.append({
                                        "display": name,
                                        "insert": name,
                                        "replace": len(arg_tok),
                                    })
                    except Exception:
                        pass

            elif cmd in ('/spawn', '/spawnAutoApprovedChildThread'):
                # Filesystem path suggestions
                if arg_tok:
                    expanded = os.path.expanduser(arg_tok)
                    base_dir = os.path.dirname(expanded) or '.'
                    needle = os.path.basename(expanded)
                    try:
                        if os.path.isdir(base_dir):
                            entries = os.listdir(base_dir)
                            for name in sorted(entries)[:20]:
                                if needle and not name.lower().startswith(needle.lower()):
                                    continue
                                path = os.path.join(base_dir, name)
                                suffix = '/' if os.path.isdir(path) else ''
                                full_path = path + suffix
                                suggestions.append({
                                    "display": name + suffix,
                                    "insert": full_path,
                                    "replace": len(arg_tok),
                                })
                    except:
                        pass

            elif cmd == '/updateAllModels':
                # Provider name suggestions
                providers = ['openai', 'anthropic', 'google', 'deepseek', 'openrouter', 'xai']
                arg_lower = arg_tok.lower()
                for p in providers:
                    if not arg_lower or arg_lower in p.lower():
                        suggestions.append({
                            "display": p,
                            "insert": p,
                            "replace": len(arg_tok),
                        })

            elif cmd in ('/disableTool', '/enableTool'):
                # Tool name suggestions
                tool_names = ['bash', 'computer', 'text_editor', 'mcp']
                arg_lower = arg_tok.lower()
                for name in tool_names:
                    if not arg_lower or arg_lower in name.lower():
                        suggestions.append({
                            "display": name,
                            "insert": name,
                            "replace": len(arg_tok),
                        })

            elif cmd == '/toolsSecrets':
                # on/off suggestions
                for opt in ['on', 'off']:
                    if not arg_tok or arg_tok.lower() in opt:
                        suggestions.append({
                            "display": opt,
                            "insert": opt,
                            "replace": len(arg_tok),
                        })

            elif cmd == '/theme':
                # Theme name suggestions
                arg_lower = arg_tok.lower()
                for theme in THEMES:
                    if not arg_lower or arg_lower in theme.lower():
                        suggestions.append({
                            "display": theme,
                            "insert": theme,
                            "replace": len(arg_tok),
                        })

            elif cmd == '/togglePanel':
                # Panel name suggestions
                for panel in ['chat', 'children', 'system']:
                    if not arg_tok or arg_tok.lower() in panel:
                        suggestions.append({
                            "display": panel,
                            "insert": panel,
                            "replace": len(arg_tok),
                        })

            elif cmd == '/enterMode':
                # Mode suggestions
                for mode in ['send', 'newline']:
                    if not arg_tok or arg_tok.lower() in mode:
                        suggestions.append({
                            "display": mode,
                            "insert": mode,
                            "replace": len(arg_tok),
                        })

            elif cmd == '/continue':
                # Message ID suggestions from current thread
                # Show messages in reverse order (most recent first) so user can pick continue point

                # Handle named argument: extract value after msg_id=
                search_term = arg_tok
                replace_len = len(arg_tok)
                if 'msg_id=' in arg:
                    match = re.search(r'msg_id=(\S*)$', arg)
                    if match:
                        search_term = match.group(1)
                        replace_len = len(search_term)

                search_lower = search_term.lower()

                if thread_id:
                    t = core.db.get_thread(thread_id)
                    if t and t.snapshot_json:
                        try:
                            snap = json.loads(t.snapshot_json)
                            msgs = snap.get('messages', []) or []
                            # Reverse order: most recent messages first
                            for msg in reversed(msgs):
                                msg_id = msg.get('msg_id', '')
                                if not msg_id:
                                    continue
                                role = msg.get('role', 'unknown')
                                content = msg.get('content', '') or ''
                                # Truncate content for display
                                content_preview = content[:40].replace('\n', ' ')
                                if len(content) > 40:
                                    content_preview += '...'

                                # Build searchable string
                                hay = f"{msg_id} {role} {content}".lower()
                                if search_lower and search_lower not in hay:
                                    continue

                                # Build display: [msg_id_short] <role> content_preview
                                display = f"[{msg_id[-8:]}] <{role}> {content_preview}"
                                suggestions.append({
                                    "display": display,
                                    "insert": msg_id,
                                    "replace": replace_len,
                                })
                                if len(suggestions) >= 30:
                                    break
                        except Exception:
                            pass

            elif cmd == '/duplicateThread':
                # Message ID suggestions for /duplicateThread
                # Format: /duplicateThread [name] [msg_id] or /duplicateThread name=<n> msg_id=<id>
                # Suggest message IDs when it looks like we're typing the msg_id argument

                # Handle named argument: extract value after msg_id=
                search_term = arg_tok
                replace_len = len(arg_tok)
                if 'msg_id=' in arg:
                    # Find the value after msg_id=
                    match = re.search(r'msg_id=(\S*)$', arg)
                    if match:
                        search_term = match.group(1)
                        replace_len = len(search_term)

                search_lower = search_term.lower()

                # Check if we're likely in msg_id position (second positional or after msg_id=)
                parts = arg.split()
                in_msg_id_position = len(parts) >= 1 or 'msg_id=' in arg

                if in_msg_id_position and thread_id:
                    t = core.db.get_thread(thread_id)
                    if t and t.snapshot_json:
                        try:
                            snap = json.loads(t.snapshot_json)
                            msgs = snap.get('messages', []) or []
                            # Show messages in order (oldest first for duplicate - picking a checkpoint)
                            for msg in msgs:
                                msg_id = msg.get('msg_id', '')
                                if not msg_id:
                                    continue
                                role = msg.get('role', 'unknown')
                                content = msg.get('content', '') or ''
                                content_preview = content[:40].replace('\n', ' ')
                                if len(content) > 40:
                                    content_preview += '...'

                                hay = f"{msg_id} {role} {content}".lower()
                                if search_lower and search_lower not in hay:
                                    continue

                                display = f"[{msg_id[-8:]}] <{role}> {content_preview}"
                                suggestions.append({
                                    "display": display,
                                    "insert": msg_id,
                                    "replace": replace_len,
                                })
                                if len(suggestions) >= 30:
                                    break
                        except Exception:
                            pass

    # Shell command completion ($ prefix)
    elif prefix.startswith('$'):
        # Could add shell command suggestions here
        pass

    # Regular text - filesystem paths and conversation words
    elif prefix:
        tok = last_token(prefix)
        fs_suggestions = []

        # Try filesystem completion first (like egg.py)
        if tok:
            expanded = os.path.expanduser(tok)
            base_dir = expanded
            needle = ''
            if not os.path.isdir(expanded):
                base_dir = os.path.dirname(expanded) or '.'
                needle = os.path.basename(expanded)
            try:
                if os.path.isdir(base_dir):
                    entries = os.listdir(base_dir)
                    for name in sorted(entries):
                        if needle and not name.lower().startswith(needle.lower()):
                            continue
                        path = os.path.join(base_dir, name)
                        suffix = '/' if os.path.isdir(path) else ''
                        full_path = path + suffix
                        fs_suggestions.append({
                            "display": name + suffix,
                            "insert": full_path,
                            "replace": len(tok),
                        })
                        if len(fs_suggestions) >= 20:
                            break
            except:
                pass

        # If filesystem found matches, use those
        if fs_suggestions:
            suggestions.extend(fs_suggestions)
        # Otherwise, fall back to conversation word completion
        elif thread_id and tok and len(tok) >= 2:
            t = core.db.get_thread(thread_id)
            if t and t.snapshot_json:
                try:
                    snap = json.loads(t.snapshot_json)
                    msgs = snap.get('messages', []) or []
                    words = set()
                    tok_lower = tok.lower()
                    for msg in msgs[-100:]:  # Last 100 messages
                        content = msg.get('content') or ''
                        if isinstance(content, str):
                            for word in re.findall(r"[A-Za-z0-9_]{3,}", content):
                                if word.lower().startswith(tok_lower) and word.lower() != tok_lower:
                                    words.add(word)
                    for word in sorted(words)[:15]:
                        suggestions.append({
                            "display": word,
                            "insert": word,
                            "replace": len(tok),
                        })
                except:
                    pass

    return {"suggestions": suggestions[:20]}  # Limit total
