"""Autocomplete logic for eggw backend."""
from __future__ import annotations

import json
import re
from typing import Optional, List

from fastapi import APIRouter

from eggthreads import list_threads, create_default_tools
from eggthreads.artifact_completion import (
    artifact_workspace_from_db,
    filesystem_completion_items,
    is_provider_artifact_export_path_position,
    is_provider_artifact_id_position,
    provider_artifact_completion_items,
)
from eggthreads.content_parts import content_to_plain_text
from eggthreads.command_catalog import (
    CommandContext,
    EGGW_COMMAND_COMPLETIONS,
    SESSION_ON_COMPLETIONS,
    SESSION_TARGET_COMPLETIONS,
    create_default_command_registry,
)
from eggthreads.completion_catalog import global_completion_items, merge_completion_items, thread_completion_items
from eggthreads.image_generation import complete_image_generate_args
from eggthreads.skills import list_skills

from .. import core
from ..theme_registry import THEMES


def get_tool_names() -> List[str]:
    """Get list of available tool names from the registry."""
    try:
        registry = create_default_tools()
        return sorted(registry._tools.keys())
    except Exception:
        return []


def _filesystem_suggestions(token: str, *, limit: int = 20, thread_id: str | None = None) -> List[dict]:
    try:
        from eggthreads import get_thread_working_directory

        working_dir = get_thread_working_directory(core.db, thread_id) if core.db and thread_id else None
    except Exception:
        working_dir = None
    return filesystem_completion_items(token, limit=limit, working_dir=working_dir)

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
            commands = EGGW_COMMAND_COMPLETIONS
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

            try:
                registry_items = create_default_command_registry().complete(
                    cmd,
                    CommandContext(db=core.db, current_thread=thread_id),
                    arg,
                )
            except KeyError:
                registry_items = []
            # The shared terminal command still names its middle panel
            # "children". EggW replaced that surface with the full Threads
            # drawer, so expose the browser's current name here.
            if cmd == '/togglePanel':
                registry_items = [
                    "threads" if item == "children" else item
                    for item in registry_items
                ]
            if registry_items:
                suggestions.extend(
                    dict(item)
                    if isinstance(item, dict)
                    else {
                        "display": str(item),
                        "insert": str(item),
                        "replace": len(arg_tok),
                    }
                    for item in registry_items
                )
                generic = global_completion_items(core.db, thread_id, prefix, limit=20)
                return {"suggestions": merge_completion_items(suggestions, generic, limit=20)}

            if cmd == '/model':
                # Model name suggestions - replace entire argument (supports multi-word search)
                # Also searches provider:model format so "openai-pro" finds its models
                # Strip trailing whitespace from arg for matching
                arg_stripped = arg.rstrip()
                if arg_stripped:
                    # Split into words and check if all words are found in the model name
                    # or in the provider:model string
                    words = arg_stripped.lower().split()
                    for key in sorted(core.chat_model_keys(core.models_config, core.llm_client)):
                        cfg = core.models_config.get(key, {})
                        provider = cfg.get("provider", "")
                        searchable = f"{provider}:{key}".lower()
                        if all(w in key.lower() or w in searchable for w in words):
                            suggestions.append({
                                "display": key,
                                "insert": key,
                                "replace": len(arg_stripped),  # Replace entire argument
                                "meta": provider,
                            })
                else:
                    # No argument - show all models
                    for key in sorted(core.chat_model_keys(core.models_config, core.llm_client)):
                        cfg = core.models_config.get(key, {})
                        provider = cfg.get("provider", "")
                        suggestions.append({
                            "display": key,
                            "insert": key,
                            "replace": 0,
                            "meta": provider,
                        })

            elif cmd in ('/thread', '/deleteThread', '/waitForThreads'):
                streaming_roots = set()
                try:
                    from eggthreads.runner import scheduler_task_is_live

                    streaming_roots = {
                        root_id
                        for root_id, entry in core.active_schedulers.items()
                        if isinstance(entry, dict) and scheduler_task_is_live(entry.get("task"))
                    }
                except Exception:
                    pass
                streaming_threads = set()
                for candidate in list_threads(core.db):
                    try:
                        root_id = core.get_thread_root_id(candidate.thread_id)
                    except Exception:
                        continue
                    if root_id in streaming_roots:
                        streaming_threads.add(candidate.thread_id)
                suggestions.extend(thread_completion_items(
                    core.db,
                    arg_tok,
                    current_thread=thread_id,
                    match_metadata=True,
                    include_empty=True,
                    include_streaming=True,
                    streaming_thread_ids=streaming_threads,
                    limit=50,
                ))

            elif cmd == '/skill':
                arg_lower = arg_tok.lower()
                for skill in list_skills():
                    hay = f"{skill.name} {skill.title} {skill.description}".lower()
                    if arg_lower and arg_lower not in hay:
                        continue
                    suggestions.append({
                        "display": skill.name,
                        "insert": skill.name,
                        "replace": len(arg_tok),
                        "meta": skill.description,
                    })

            elif cmd in ('/spawnChildThread', '/spawnAutoApprovedChildThread'):
                # Filesystem path suggestions
                suggestions.extend(_filesystem_suggestions(arg_tok, limit=20, thread_id=thread_id))

            elif cmd == '/attach':
                suggestions.extend(_filesystem_suggestions(arg_tok, limit=20, thread_id=thread_id))

            elif cmd in ('/attachOutput', '/saveProviderArtifact', '/saveProviderOutput'):
                if is_provider_artifact_id_position(cmd, arg):
                    suggestions.extend(
                        provider_artifact_completion_items(
                            artifact_workspace_from_db(core.db),
                            core.db,
                            thread_id,
                            arg_tok,
                            limit=50,
                        )
                    )
                elif is_provider_artifact_export_path_position(cmd, arg):
                    path_tok = '' if arg.endswith((' ', '\t')) else arg_tok
                    suggestions.extend(_filesystem_suggestions(path_tok, limit=20, thread_id=thread_id))

            elif cmd == '/imageGenerate':
                current = last_token(arg) if not arg.endswith((' ', '\t')) else ''
                for item in complete_image_generate_args(
                    arg,
                    image_generation_models_path=core.IMAGE_GENERATION_MODELS_PATH,
                    models_path=core.MODELS_PATH,
                ):
                    replace_len = len(current)
                    suggestions.append({
                        "display": item,
                        "insert": item,
                        "replace": replace_len,
                    })

            elif cmd == '/updateAllModels':
                # Provider name suggestions
                try:
                    if core.llm_client is not None:
                        providers = sorted(core.llm_client.get_providers() or [])
                    else:
                        providers = sorted({
                            cfg.get('provider')
                            for cfg in core.models_config.values()
                            if isinstance(cfg, dict) and cfg.get('provider')
                        })
                except Exception:
                    providers = []
                arg_lower = arg_tok.lower()
                for p in providers:
                    if not arg_lower or arg_lower in p.lower():
                        suggestions.append({
                            "display": p,
                            "insert": p,
                            "replace": len(arg_tok),
                        })

            elif cmd in ('/disableTool', '/enableTool', '/toolInfo'):
                # Tool name suggestions from actual registry
                tool_names = get_tool_names()
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

            elif cmd in ('/sessionStop', '/sessionReset'):
                for opt in SESSION_TARGET_COMPLETIONS:
                    if not arg_tok or arg_tok.lower() in opt:
                        suggestions.append({
                            "display": opt,
                            "insert": opt,
                            "replace": len(arg_tok),
                        })

            elif cmd == '/sessionOn':
                for opt in SESSION_ON_COMPLETIONS:
                    if not arg_tok or arg_tok.lower() in opt.lower():
                        suggestions.append({
                            "display": opt,
                            "insert": opt,
                            "replace": len(arg_tok),
                        })

            elif cmd == '/sessionCleanup':
                for opt in ['dry-run', 'apply', 'older_than=1h', 'older_than=1d']:
                    if not arg_tok or arg_tok.lower() in opt.lower():
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

            elif cmd == '/displayVerbosity':
                for level in ['max', 'medium', 'min']:
                    if not arg_tok or arg_tok.lower() in level:
                        suggestions.append({
                            "display": level,
                            "insert": level,
                            "replace": len(arg_tok),
                        })

            elif cmd == '/togglePanel':
                # Panel name suggestions
                for panel in ['chat', 'threads', 'system']:
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

            elif cmd == '/setThreadPriority':
                # Check if we're completing after thread=
                if 'thread=' in arg:
                    match = re.search(r'thread=(\S*)$', arg)
                    if match:
                        # Complete thread ID after thread=
                        search_term = match.group(1)
                        search_lower = search_term.lower()
                        threads = list_threads(core.db)
                        try:
                            threads.sort(key=lambda t: t.created_at or '', reverse=True)
                        except Exception:
                            pass
                        for t in threads[:50]:
                            tid = t.thread_id
                            name = t.name or ''
                            recap = t.short_recap or ''
                            hay = f"{tid} {name} {recap}".lower()
                            if search_lower and search_lower not in hay:
                                continue
                            display = f"{tid[-8:]} - {recap[:30]}" if recap else tid[-8:]
                            if name:
                                display += f" ({name})"
                            suggestions.append({
                                "display": display,
                                "insert": tid,
                                "replace": len(search_term),
                            })
                else:
                    # Suggest parameter names
                    params = ['priority=', 'threshold=', 'apiTimeout=', 'thread=']
                    arg_lower = arg_tok.lower()
                    for param in params:
                        if not arg_lower or arg_lower in param.lower():
                            suggestions.append({
                                "display": param,
                                "insert": param,
                                "replace": len(arg_tok),
                            })

            elif cmd in ('/continue', '/compact'):
                # Message ID suggestions from current thread
                # Show messages in reverse order (most recent first) so user can pick continue point

                if cmd == '/compact':
                    for selector in ('last_user', 'last_llm'):
                        if not arg_tok or arg_tok.lower() in selector:
                            suggestions.append({
                                "display": selector,
                                "insert": selector,
                                "replace": len(arg_tok),
                            })

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
                                content = content_to_plain_text(msg.get('content', ''))
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
                                content = content_to_plain_text(msg.get('content', ''))
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
        generic = global_completion_items(core.db, thread_id, prefix, limit=20)

        # If filesystem found matches, use those
        if generic:
            suggestions.extend(generic)
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
                        content = content_to_plain_text(msg.get('content'))
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
                except Exception:
                    pass

    generic = global_completion_items(core.db, thread_id, prefix, limit=20)
    return {"suggestions": merge_completion_items(suggestions, generic, limit=20)}
