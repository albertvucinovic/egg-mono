# Compaction Checkpoint Skill

Use this skill when the provider context starts immediately after an Egg thread compaction and the prompt asks for a continuation checkpoint.

## Goal

Create a concise durable checkpoint of the work state, then choose whether to stop or continue based on the compaction mode.

The checkpoint should preserve:

- the pending user request or task;
- Assistant Notes / interim answers, especially manager-worker progress notes;
- important decisions, invariants, and design constraints;
- files changed or intended to change;
- commands/tests already run and their results;
- known failures, risks, or unresolved questions;
- exact next steps.

Use hydrated thread-history helpers when needed (`all_messages`, `current_prompt_messages`, `older_messages_not_in_prompt`, `messages_by_id`, `search_thread(...)`, `get_message(...)`, `print_message(...)`, `reload_thread_context()`).

## First-pass narrative skeleton

Before writing the checkpoint, execute the complete embedded script below in the hydrated `python_repl`. It builds a bounded evidence map from the preinstalled `thread_context` and helper globals. The normal execution path is **not** `bash`, a standalone Python process, or an import from Egg internals; those environments do not have the caller thread's hydrated context. The map is a discovery aid, not the checkpoint itself.

How the script source reaches `python_repl` is only a transport choice. **Using `extract_tool_output` or creating a file is optional.** If the complete fence is already available, pass it directly. When the `skill` wrapper is enabled inside the hydrated REPL, this short loader avoids copying the long fence and does not create an artifact or project file:

```python
from eggtools import skill as load_skill

_document = load_skill("compaction-checkpoint")
_opening = "```python\n# egg-compaction-narrative-skeleton\n"
if _opening not in _document:
    raise RuntimeError("compaction narrative script marker not found")
_script = _document.split(_opening, 1)[1].split("\n```", 1)[0]
exec(compile(_script, "<skill:compaction-checkpoint>", "exec"), globals(), globals())
```

Use the extraction fallback documented after the script only when direct transfer or the in-REPL loader is unavailable or unreliable.

The renderer deliberately separates two kinds of evidence:

1. **Historical conversation — user-centered.** It retains most actionable user messages, plus a small selection of useful Assistant messages and Assistant Notes. Historical tool calls/results and synthetic compaction controls are excluded.
2. **Active turn — continuation-centered.** It starts with the latest consecutive actionable user-message burst and retains subsequent Assistant messages, Assistant Notes, operational system messages, and tool calls/results. A later synthetic compaction-control request and the checkpoint machinery it starts are outside this turn.

The visible map favors readable conversation over database indexing. Exact message/tool identifiers and search cues remain available in the persistent `compaction_narrative_skeleton_index`; use `search_thread(...)` for ordinary expansion. The renderer also uses readable `content_text` for structured attachment content and enforces hard character/line limits.

Do not summarize only the current provider-prompt tail. Hidden/local-only, deleted, and `/continue`-erased content is intentionally absent from the hydrated view; do not bypass that visibility boundary with raw database inspection during ordinary checkpointing.

```python
# egg-compaction-narrative-skeleton
import re
from itertools import islice

MAX_OUTPUT_CHARS = 48_000
MAX_OUTPUT_LINES = 700
MAX_CAPTURED_TEXT = 12_000
MAX_HISTORY_USERS = 360
MAX_HISTORY_CONTEXT = 48
MAX_ACTIVE_ENTRIES = 220
SKELETON_VERSION = 3
SKELETON_START_MARKER = "THREAD NARRATIVE SKELETON FOR COMPACTION v3"
SKELETON_END_MARKER = "END THREAD NARRATIVE SKELETON FOR COMPACTION v3"
NOTE_TOOLS = {
    "answer_user_while_preserving_llm_turn",
    "get_user_message_while_preserving_llm_turn",
}
CONTROL_RE = re.compile(
    r"^Use the `compaction-checkpoint` skill\. Mode: `(summary_only|checkpoint_and_resume)`\.$"
)
IMPORTANT_RE = re.compile(
    r"\b(error|fail(?:ed|ure)?|traceback|interrupt(?:ed)?|cancel(?:led|ed)?|"
    r"commit(?:ted)?|test(?:s|ed|ing)?|pass(?:ed)?|skip(?:ped)?|warning|risk|"
    r"todo|next step|unresolved|dirty|clean|branch|sha|artifact|decid(?:e|ed|ing)|"
    r"must|should|constraint|require(?:d|ment)?|do not|don't)\b",
    re.IGNORECASE,
)


def _as_int(value):
    try:
        return int(value)
    except Exception:
        return None


def _msg_id(message):
    return str(message.get("msg_id") or message.get("id") or "")


def _clip_field(value, limit):
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    if limit < 5:
        return text[:limit]
    head = max(1, (limit - 1) * 2 // 3)
    return text[:head] + "…" + text[-(limit - head - 1):]


def _structure_truncated(value, depth=0):
    if isinstance(value, dict):
        if depth >= 2:
            return bool(value)
        return len(value) > 12 or any(
            _structure_truncated(item, depth + 1)
            for item in islice(value.values(), 12)
        )
    if isinstance(value, (list, tuple)):
        if depth >= 2:
            return bool(value)
        return len(value) > 12 or any(
            _structure_truncated(item, depth + 1)
            for item in islice(iter(value), 12)
        )
    return False


def _bounded_value(value, depth=0):
    """Readable bounded fallback without serializing an unbounded structure."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return repr(value)
    if depth >= 2:
        try:
            return f"<{type(value).__name__} len={len(value)}>"
        except Exception:
            return f"<{type(value).__name__}>"
    if isinstance(value, dict):
        items = list(islice(value.items(), 12))
        body = ", ".join(
            f"{_clip_field(key, 80)}={_bounded_value(item, depth + 1)}"
            for key, item in items
        )
        suffix = ", …" if len(value) > len(items) else ""
        return "{" + body + suffix + "}"
    if isinstance(value, (list, tuple)):
        items = list(islice(iter(value), 12))
        body = ", ".join(_bounded_value(item, depth + 1) for item in items)
        suffix = ", …" if len(value) > len(items) else ""
        return "[" + body + suffix + "]"
    return _clip_field(repr(value), MAX_CAPTURED_TEXT)


def _head_tail(text, limit):
    if len(text) <= limit:
        return text, False
    if limit <= 1:
        return "…"[:limit], True
    head = max(1, (limit - 1) * 2 // 3)
    return text[:head] + "…" + text[-(limit - head - 1):], True


def _one_line(value):
    structure_truncated = _structure_truncated(value)
    raw = _bounded_value(value)
    raw, source_truncated = _head_tail(raw, MAX_CAPTURED_TEXT)
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    raw = re.sub(r"[\n\v\f\x1c-\x1e\x85\u2028\u2029]+", " ↩ ", raw)
    raw = re.sub(r"[ \t]+", " ", raw).strip()
    return raw, bool(source_truncated or structure_truncated)


def _message_text(message):
    prepared = message.get("content_text")
    if isinstance(prepared, str):
        return _one_line(prepared)
    return _one_line(message.get("content", ""))


def _tool_calls(message):
    calls = message.get("tool_calls")
    return calls if isinstance(calls, list) else []


def _call_parts(call):
    if not isinstance(call, dict):
        return "unknown", "", call
    function = call.get("function")
    function = function if isinstance(function, dict) else {}
    name = function.get("name") or call.get("name") or "unknown"
    call_id = call.get("id") or call.get("tool_call_id") or ""
    arguments = function.get("arguments", call.get("arguments", ""))
    return str(name), str(call_id), arguments


def _control_mode(message, text):
    if message.get("message_kind") == "compaction_control" or message.get("compaction_summary_request"):
        if message.get("compaction_mode") in {"summary_only", "checkpoint_and_resume"}:
            return message.get("compaction_mode")
        return "checkpoint_and_resume" if message.get("auto_compaction_request") else "summary_only"
    match = CONTROL_RE.fullmatch(text)
    return match.group(1) if match else None


def _is_note(message, note_call_ids):
    if message.get("role") != "assistant":
        return False
    if message.get("message_kind") == "assistant_note" or message.get("answer_user_preserve_turn"):
        return True
    if message.get("source_tool_name") in NOTE_TOOLS or message.get("awaiting_user_message_tool_call_id"):
        return True
    call_id = str(message.get("tool_call_id") or "")
    return bool(call_id and call_id in note_call_ids and not _tool_calls(message))


def _preview(text, limit, *, tail_worthy=False):
    if limit <= 0 or not text:
        return ""
    if len(text) <= limit:
        return text
    if limit <= 1:
        return "…"[:limit]
    if not tail_worthy or limit < 24:
        return text[: limit - 1].rstrip() + "…"
    head = max(1, (limit - 3) * 2 // 3)
    tail = max(1, limit - head - 3)
    return text[:head].rstrip() + " … " + text[-tail:].lstrip()


def _allocate_previews(records, budget, minimum, desired, *, recent_count=0):
    """Allocate readable text fairly, then spend remaining space on recent entries."""
    if not records:
        return {}, 0
    label_cost = sum(len(record["label"]) + 2 for record in records)
    text_budget = max(len(records), budget - label_cost - len(records))
    base = max(24, min(minimum, text_budget // len(records)))
    limits = {id(record): min(len(record["text"]), base) for record in records}
    spent = sum(limits.values())
    order = list(records)
    if recent_count:
        recent = order[-recent_count:]
        older = order[:-recent_count]
        order = list(reversed(recent)) + list(reversed(older))
    else:
        order = list(reversed(order))
    remaining = max(0, text_budget - spent)
    for record in order:
        target = min(len(record["text"]), desired(record))
        extra = min(remaining, max(0, target - limits[id(record)]))
        limits[id(record)] += extra
        remaining -= extra
        if remaining <= 0:
            break
    previews = {
        id(record): _preview(
            record["text"],
            limits[id(record)],
            tail_worthy=record["kind"] == "RESULT",
        )
        for record in records
    }
    shortened = sum(previews[id(record)] != record["text"] for record in records)
    return previews, shortened


def _index_record(record):
    return {
        "kind": record["kind"],
        "msg_id": record["msg_id"],
        "event_seq": record["seq"],
        "message_index": record["message_index"],
        "tool_name": record.get("tool_name", ""),
        "call_id": record.get("call_id", ""),
        "source_truncated": record["source_truncated"],
        "search_cue": _preview(record["text"], 160),
    }


def _select_history_users(history_users):
    if len(history_users) <= MAX_HISTORY_USERS:
        return list(history_users)
    first_count = min(24, MAX_HISTORY_USERS // 6)
    return list(history_users[:first_count]) + list(history_users[-(MAX_HISTORY_USERS - first_count):])


def _select_history_context(episodes):
    candidates = []
    recent_start = max(0, len(episodes) - 24)
    for episode_index, episode in enumerate(episodes):
        assistant = next((item for item in reversed(episode["context"]) if item["kind"] == "ASST"), None)
        note = next((item for item in reversed(episode["context"]) if item["kind"] == "NOTE"), None)
        recent = episode_index >= recent_start
        for record in (assistant, note):
            if not record or not record["text"]:
                continue
            important = bool(IMPORTANT_RE.search(record["text"]))
            if not recent and not important:
                continue
            priority = (2000 if recent else 0) + (700 if record["kind"] == "NOTE" else 500)
            if important:
                priority += 800
            priority += episode_index
            candidates.append((priority, record))
    selected = [record for _priority, record in sorted(candidates, key=lambda item: item[0], reverse=True)[:MAX_HISTORY_CONTEXT]]
    return sorted(selected, key=lambda record: (record["message_index"], record["suborder"]))


def _select_active(active_records):
    if len(active_records) <= MAX_ACTIVE_ENTRIES:
        return list(active_records), 0

    mandatory = [record for record in active_records if record["kind"] not in {"CALL", "RESULT"}]
    if len(mandatory) >= MAX_ACTIVE_ENTRIES:
        users = [record for record in mandatory if record["kind"] == "USER"]
        others = [record for record in mandatory if record["kind"] != "USER"]
        selected = users + others[-max(0, MAX_ACTIVE_ENTRIES - len(users)):]
        selected_ids = {id(record) for record in selected}
        return (
            [record for record in active_records if id(record) in selected_ids],
            len(active_records) - len(selected_ids),
        )

    capacity = MAX_ACTIVE_ENTRIES - len(mandatory)
    groups = {}
    group_order = []
    for record in active_records:
        if record["kind"] not in {"CALL", "RESULT"}:
            continue
        key = record["call_id"] or f"orphan:{record['message_index']}:{record['suborder']}"
        if key not in groups:
            groups[key] = []
            group_order.append(key)
        groups[key].append(record)

    ranked = []
    for order, key in enumerate(group_order):
        group = groups[key]
        important = any(IMPORTANT_RE.search(record["text"]) for record in group)
        ranked.append(((100_000 if important else 0) + order, key, group))

    chosen = []
    used = 0
    for _priority, _key, group in sorted(ranked, reverse=True):
        if used + len(group) > capacity:
            continue
        chosen.extend(group)
        used += len(group)
        if used >= capacity:
            break
    selected_ids = {id(record) for record in mandatory + chosen}
    selected = [record for record in active_records if id(record) in selected_ids]
    return selected, len(active_records) - len(selected)


def _build_output(context):
    messages = [
        message
        for message in (context.get("all_messages") or globals().get("all_messages", []) or [])
        if isinstance(message, dict)
    ]

    call_names = {}
    note_call_ids = set()
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for call in _tool_calls(message):
            name, call_id, _arguments = _call_parts(call)
            if call_id:
                call_names[call_id] = name
                if name in NOTE_TOOLS:
                    note_call_ids.add(call_id)

    records = []
    control_records = []
    omitted_empty_assistant = 0
    valid_sequences = []

    def add_record(message, message_index, suborder, kind, text, source_truncated=False, *, tool_name="", call_id="", control_mode=None):
        seq = _as_int(message.get("event_seq"))
        if seq is not None:
            valid_sequences.append(seq)
        record = {
            "kind": kind,
            "msg_id": _msg_id(message),
            "seq": seq,
            "message_index": message_index,
            "suborder": suborder,
            "text": text,
            "source_truncated": bool(source_truncated),
            "tool_name": str(tool_name or ""),
            "call_id": str(call_id or ""),
            "control_mode": control_mode,
            "label": "",
        }
        records.append(record)
        if kind == "CONTROL":
            control_records.append(record)

    for message_index, message in enumerate(messages):
        role = str(message.get("role") or "")
        text, source_truncated = _message_text(message)
        suborder = 0
        if role == "user":
            mode = _control_mode(message, text)
            add_record(
                message,
                message_index,
                suborder,
                "CONTROL" if mode else "USER",
                text or "(no readable text)",
                source_truncated,
                control_mode=mode,
            )
        elif role == "assistant":
            if _is_note(message, note_call_ids):
                add_record(message, message_index, suborder, "NOTE", text, source_truncated, call_id=message.get("tool_call_id", ""))
                suborder += 1
            elif text:
                add_record(message, message_index, suborder, "ASST", text, source_truncated)
                suborder += 1
            elif not _tool_calls(message):
                omitted_empty_assistant += 1
            for call in _tool_calls(message):
                name, call_id, arguments = _call_parts(call)
                call_text, call_truncated = _one_line(arguments)
                add_record(message, message_index, suborder, "CALL", call_text, call_truncated, tool_name=name, call_id=call_id)
                suborder += 1
        elif role == "tool":
            call_id = str(message.get("tool_call_id") or "")
            add_record(
                message,
                message_index,
                suborder,
                "RESULT",
                text,
                source_truncated,
                tool_name=message.get("name") or call_names.get(call_id, ""),
                call_id=call_id,
            )
        elif role == "system" and re.search(
            r"\b(error|failed|failure|interrupt|retry|unavailable|context limit|recovery)\b",
            text,
            re.IGNORECASE,
        ):
            add_record(message, message_index, suborder, "SYSTEM", text, source_truncated)

    actionable = [record for record in records if record["kind"] == "USER"]
    latest_actionable = actionable[-1] if actionable else None
    latest_control = control_records[-1] if control_records else None

    content_end_index = len(messages)
    if latest_actionable and latest_control and latest_control["message_index"] > latest_actionable["message_index"]:
        content_end_index = latest_control["message_index"]
    scoped_actionable = [
        record for record in actionable if record["message_index"] < content_end_index
    ]
    latest_actionable = scoped_actionable[-1] if scoped_actionable else None

    active_start_index = content_end_index
    if latest_actionable:
        active_start_index = latest_actionable["message_index"]
        cursor = active_start_index - 1
        while cursor >= 0:
            message = messages[cursor]
            if str(message.get("role") or "") != "user":
                break
            cursor_text, _truncated = _message_text(message)
            if _control_mode(message, cursor_text):
                cursor -= 1
                continue
            active_start_index = cursor
            cursor -= 1

    scoped_records = [
        record
        for record in records
        if record["kind"] != "CONTROL" and record["message_index"] < content_end_index
    ]
    history_records = [record for record in scoped_records if record["message_index"] < active_start_index]
    active_records = [record for record in scoped_records if record["message_index"] >= active_start_index]
    history_users = [record for record in history_records if record["kind"] == "USER"]
    selected_history_users = _select_history_users(history_users)

    episodes = []
    current_episode = None
    for record in history_records:
        if record["kind"] == "USER":
            current_episode = {"user": record, "context": []}
            episodes.append(current_episode)
        elif current_episode is not None and record["kind"] in {"ASST", "NOTE"} and record["text"]:
            current_episode["context"].append(record)
    selected_history_context = _select_history_context(episodes)

    shown_history_ids = {id(record) for record in selected_history_users + selected_history_context}
    history_items = [record for record in history_records if id(record) in shown_history_ids]
    for record in history_items:
        record["label"] = {
            "USER": "User",
            "ASST": "Assistant",
            "NOTE": "Assistant Note",
        }[record["kind"]]

    selected_active, omitted_active = _select_active(active_records)
    operation_numbers = {}
    next_operation = 1
    for record in active_records:
        if record["kind"] not in {"CALL", "RESULT"}:
            continue
        key = record["call_id"] or f"orphan:{record['message_index']}:{record['suborder']}"
        if key not in operation_numbers:
            operation_numbers[key] = next_operation
            next_operation += 1
    for record in selected_active:
        if record["kind"] in {"CALL", "RESULT"}:
            key = record["call_id"] or f"orphan:{record['message_index']}:{record['suborder']}"
            operation = operation_numbers[key]
            direction = "call" if record["kind"] == "CALL" else "result"
            tool = f" [{record['tool_name']}]" if record["tool_name"] else ""
            record["label"] = f"Tool {operation} {direction}{tool}"
        else:
            record["label"] = {
                "USER": "User (active)",
                "ASST": "Assistant",
                "NOTE": "Assistant Note",
                "SYSTEM": "Operational System",
            }.get(record["kind"], record["kind"].title())

    # Reserve the active turn first, then give most remaining space to historical users.
    active_previews, active_shortened = _allocate_previews(
        selected_active,
        19_000,
        180,
        lambda record: 3200 if record["kind"] == "USER" else (1800 if record["kind"] in {"ASST", "NOTE"} else 1100),
        recent_count=36,
    )
    active_lines = [f"{record['label']}: {active_previews[id(record)] or '(no readable text)'}" for record in selected_active]
    active_chars = sum(len(line) + 1 for line in active_lines)

    history_budget = max(10_000, min(26_000, 44_000 - active_chars))
    user_items = [record for record in history_items if record["kind"] == "USER"]
    context_items = [record for record in history_items if record["kind"] != "USER"]
    user_budget = int(history_budget * 0.72) if context_items else history_budget
    context_budget = history_budget - user_budget
    user_previews, user_shortened = _allocate_previews(
        user_items,
        user_budget,
        150,
        lambda _record: 1100,
        recent_count=24,
    )
    context_previews, context_shortened = _allocate_previews(
        context_items,
        context_budget,
        120,
        lambda record: 650 if record["kind"] == "NOTE" else 800,
        recent_count=20,
    )
    history_lines = []
    for record in history_items:
        preview = user_previews.get(id(record), context_previews.get(id(record), ""))
        history_lines.append(f"{record['label']}: {preview or '(no readable text)'}")

    history_tool_events = sum(record["kind"] in {"CALL", "RESULT"} for record in history_records)
    active_users = [record for record in active_records if record["kind"] == "USER"]
    control_mode = latest_control.get("control_mode") if latest_control else None
    watermark = max(valid_sequences) if valid_sequences else None
    nonmonotonic = any(left > right for left, right in zip(valid_sequences, valid_sequences[1:]))

    lines = [
        SKELETON_START_MARKER,
        "Purpose: preserve user intent across the conversation and continuation evidence for the active turn.",
        (
            f"Coverage: historical users shown={len(selected_history_users)}/{len(history_users)} "
            f"(shortened={user_shortened}); selected historical assistant/note context="
            f"{len(selected_history_context)} (shortened={context_shortened}); active users={len(active_users)}."
        ),
        (
            f"Policy: historical tool calls/results omitted={history_tool_events}; active-turn entries shown="
            f"{len(selected_active)}/{len(active_records)} (shortened={active_shortened})."
        ),
        (
            f"Hydration: messages={len(messages)} watermark_event_seq={watermark if watermark is not None else 'none'} "
            f"control_mode={control_mode or 'none'} omitted_empty_assistant={omitted_empty_assistant} "
            f"nonmonotonic_event_seq={'yes' if nonmonotonic else 'no'}."
        ),
        "Exact IDs and search cues are stored in compaction_narrative_skeleton_index; use search_thread(...) to expand visible phrases.",
        "",
        "=== HISTORICAL CONVERSATION — USER-CENTERED ===",
        "Most actionable user messages are retained. Historical tool mechanics are intentionally absent.",
    ]
    if len(selected_history_users) < len(history_users):
        lines.append(
            f"[Historical user coverage limit: {len(history_users) - len(selected_history_users)} middle user messages omitted; inspect the persistent index/search if relevant.]"
        )
    lines.extend(history_lines or ["(No historical conversation before the active user burst.)"])
    lines.extend([
        "",
        "=== ACTIVE TURN — CONTINUE FROM HERE ===",
        "Begins with the latest consecutive actionable user-message burst and keeps subsequent continuation evidence.",
    ])
    if omitted_active:
        lines.append(
            f"[Active-turn pressure limit: {omitted_active} lower-priority tool events omitted; exact retained/omitted metadata is in the persistent index.]"
        )
    lines.extend(active_lines or ["(No actionable user turn was found.)"])
    lines.extend([
        "",
        "=== EXPANSION GUIDANCE ===",
        "Search distinctive visible phrases with search_thread(...). Inspect exact source messages only when text is shortened, a decision is ambiguous, or a claim needs verification.",
        "Historical commands/results are deliberately not replayed here; verify old operational claims through targeted search or current repository inspection.",
        "",
        SKELETON_END_MARKER,
    ])

    output = "\n".join(lines) + "\n"
    if len(lines) > MAX_OUTPUT_LINES or len(output) > MAX_OUTPUT_CHARS:
        raise RuntimeError(
            f"narrative skeleton exceeded hard limit: chars={len(output)}, lines={len(lines)}"
        )

    index = {
        "version": SKELETON_VERSION,
        "watermark_event_seq": watermark,
        "control_mode": control_mode,
        "active_start_message_index": active_start_index if latest_actionable else None,
        "content_end_message_index": content_end_index,
        "history_users": [_index_record(record) for record in history_users],
        "history_users_shown": [_index_record(record) for record in selected_history_users],
        "history_context_shown": [_index_record(record) for record in selected_history_context],
        "history_tool_event_count_omitted": history_tool_events,
        "active_records": [_index_record(record) for record in active_records],
        "active_records_shown": [_index_record(record) for record in selected_active],
        "active_records_omitted": omitted_active,
    }
    return output, index


_reloader = globals().get("reload_thread_context")
if callable(_reloader):
    try:
        _context = _reloader()
    except Exception:
        _context = globals().get("thread_context", {})
else:
    _context = globals().get("thread_context", {})
_context = _context if isinstance(_context, dict) else {}
compaction_narrative_skeleton_output, compaction_narrative_skeleton_index = _build_output(_context)
print(compaction_narrative_skeleton_output, end="")
```

### Optional transport fallback: extract the script

Skip this section when direct execution or the in-REPL loader above worked. The checkpoint does not require an extracted artifact or a saved file.

If the skill output was truncated or exact transfer of the embedded script would otherwise be unreliable, load the skill with numbered presentation and extract only the script fence:

1. Load this skill with `line_numbers=true`.
2. Read the displayed opening and closing fence line numbers.
3. Call `extract_tool_output` on the skill call, using a 1-based half-open range `[start_line, end_line)`. Select the Python body only: start immediately after `````python`` and end at the closing ````` line.
4. Saving the returned artifact with `save_provider_artifact_to_file` is also optional; do it only when an inspectable file is useful. Otherwise, add/read the artifact through an available provider-artifact mechanism and execute its text in the hydrated REPL.

```text
skill(name="compaction-checkpoint", line_numbers=true)
extract_tool_output(
    start_line=<line immediately after the opening python fence>,
    end_line=<closing fence line>,
    filename="compaction_narrative_skeleton.py",
)
save_provider_artifact_to_file(artifact_id="<receipt artifact id>", path=".scratch/compaction_narrative_skeleton.py")
```

With no source selector, `extract_tool_output` selects the latest eligible prior published output, so the adjacent `skill` → extraction sequence above needs no opaque call ID. If another tool-call group intervened, use the extractor's zero-based `source_tool_call_group_offset` plus `source_tool_call_index` selectors; consult `tool_help(tool_name="extract_tool_output")` for ordering details. Displayed line-number prefixes are presentation only and are never written into extracted bytes. Always derive the range from the current numbered skill output.

Extraction does not change the execution contract. Do not run the saved file as a standalone shell/Python script. Execute its contents through the hydrated `python_repl` (for example, by passing the extracted text as the REPL's code) so the thread-context globals and helpers are present.

### Required complete-map check

Consume the script's **entire emitted evidence map** before writing the checkpoint. A head/tail preview of that map is not sufficient.

1. Confirm that the output begins with `THREAD NARRATIVE SKELETON FOR COMPACTION v3` and reaches the terminal `END THREAD NARRATIVE SKELETON FOR COMPACTION v3` marker.
2. If the tool response says the raw output was stored as an artifact, reports omitted middle content, or does not show the terminal marker, read **every** `read_long_tool_output` chunk in order before continuing. This is result consumption and is separate from the optional `extract_tool_output` script-transport fallback above.
3. If chunk reading is unavailable, inspect `compaction_narrative_skeleton_output` from the same persistent REPL in bounded, non-overlapping line slices until the terminal marker is reached.

Reading the complete map does not mean expanding every source message in the thread. The readable map is the bounded index; the next section determines which underlying messages require full inspection.

### Required follow-up inspection

After the first pass:

1. Read the historical user messages and the separately marked `ACTIVE TURN — CONTINUE FROM HERE`; never treat the synthetic compaction control as the task.
2. Inspect shortened or decision-critical messages with `search_thread(...)`, `get_message(...)`, or `print_message(...)`. `compaction_narrative_skeleton_index` contains exact IDs, tool correlations, coverage, and search cues without crowding the visible narrative.
3. In the active turn, correlate each tool call with its result. A call proves intent, not success. Never claim a test passed, a file changed, or a commit exists without a result or independent verification.
4. Treat Assistant Notes as reported progress, not automatically verified fact. Preserve manager/worker notes because they may contain the only durable handoff.
5. Historical tool traffic is intentionally absent. If an older operational claim is decision-critical, locate it with a distinctive visible phrase/search cue or independently inspect the current repository state rather than replaying every old tool event.
6. If the watermark may be stale because a concurrent user message arrived, run a fresh `python_repl` evaluation. Memory-session `reload_thread_context()` refreshes in-eval; Docker hydration refreshes at the start of a new evaluation.
7. Only then write the checkpoint.

## Checkpoint contract

Prefer this durable structure when a section is relevant:

- **Active request(s):** the actual pending user task, excluding `CONTROL` instructions.
- **Verified current state:** completed work backed by tool results or repository inspection.
- **Reported progress:** Assistant Notes or prior assistant claims not independently rechecked.
- **Decisions and constraints:** accepted invariants, rejected approaches, and user requirements.
- **Files:** verified changed files versus intended-but-unmodified files.
- **Commands and tests:** exact commands, outcomes, relevant failures, and interrupted/unknown results.
- **Risks and unresolved questions:** provider/runner failures, partial work, stale evidence, and assumptions.
- **Exact next steps:** ordered actions and required validation.

Be concise but specific. Distinguish **verified**, **reported**, and **planned/inferred** state. Do not replay the transcript, invent tool outcomes, or omit a known failure merely because a later retry succeeded.

## Modes

### `summary_only`

Use this mode for user-initiated/manual/handoff compaction, including `/compactWithSummary` unless the prompt explicitly says otherwise.

Behavior:

1. Write the checkpoint as normal assistant content.
2. Do not continue the task in the same turn.

### `checkpoint_and_resume`

Use this mode for recovery compaction, such as context-length exhaustion while the assistant was trying to continue work or an unhandled user message that arrived during assistant streaming.

Behavior:

1. Call `answer_user_while_preserving_llm_turn` with the checkpoint summary.
2. Do not treat that checkpoint as the final answer to the task.
3. Continue from the current actionable state after the checkpoint.
4. If there is a newer unhandled user message that arrived during the interrupted work, handle that user message before resuming older work.
5. Do not fabricate tool results. If complete assistant tool calls were persisted, let the tool-call state machine handle them; if only partial tool-call deltas existed, resume from the last stable user/task state.

