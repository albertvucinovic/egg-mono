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

Before writing the checkpoint, pass the complete embedded script below to the hydrated `python_repl`. It builds a bounded evidence map from the preinstalled `thread_context` and helper globals. The normal execution path is **not** `bash`, a standalone Python process, or an import from Egg internals; those environments do not have the caller thread's hydrated context. The map is a discovery aid, not the checkpoint itself.

The renderer deliberately:

- separates the synthetic compaction `CONTROL` request from actionable `USER` messages;
- treats Assistant Notes as first-class rows and correlates legacy hydrated notes with their note-producing tool calls;
- expands persisted tool declarations/results into `CALL` and `RESULT` evidence rows;
- includes post-boundary or error-like operational `SYSTEM` rows while excluding the standing system prompt;
- prefers current, post-boundary, recent, and error-bearing evidence under pressure;
- emits omission markers, full message IDs, a hydration watermark, and exact current-prompt/boundary scope;
- uses readable `content_text` for structured attachment content when available;
- enforces hard character and line limits on the complete output.

Do not summarize only the current provider-prompt tail. Hidden/local-only, deleted, and `/continue`-erased content is intentionally absent from the hydrated view; do not bypass that visibility boundary with raw database inspection during ordinary checkpointing.

```python
import re
import sys
from collections import Counter
from itertools import islice

MAX_OUTPUT_CHARS = 48_000
MAX_OUTPUT_LINES = 700
MAX_SELECTED_ROWS = 520
MAX_CAPTURED_TEXT = 12_000
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
    r"todo|next step|unresolved|dirty|clean|branch|sha|artifact)\b",
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


def _current_compaction(context):
    current = [
        item
        for item in (context.get("compactions") or [])
        if isinstance(item, dict) and item.get("is_current")
    ]
    return current[-1] if current else {}


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


def _counter_text(counter):
    return ",".join(f"{key}:{counter[key]}" for key in sorted(counter)) or "none"


def _main():
    reloader = globals().get("reload_thread_context")
    if callable(reloader):
        try:
            context = reloader()
        except Exception:
            context = globals().get("thread_context", {})
    else:
        context = globals().get("thread_context", {})
    context = context if isinstance(context, dict) else {}
    messages = [
        message
        for message in (context.get("all_messages") or globals().get("all_messages", []) or [])
        if isinstance(message, dict)
    ]
    prompt_messages = [
        message
        for message in (context.get("current_prompt_messages") or globals().get("current_prompt_messages", []) or [])
        if isinstance(message, dict)
    ]
    compaction = _current_compaction(context)
    boundary_seq = _as_int(compaction.get("current_prompt_starts_at_event_seq"))
    prompt_ids = {_msg_id(message) for message in prompt_messages if _msg_id(message)}
    prompt_seqs = {
        seq for seq in (_as_int(message.get("event_seq")) for message in prompt_messages)
        if seq is not None
    }

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

    rows = []
    excluded_system_messages = 0
    omitted_empty_assistant = 0
    valid_sequences = []

    def add_row(message, kind, text, source_truncated=False, *, tool_name="", call_id=""):
        seq = _as_int(message.get("event_seq"))
        if seq is not None:
            valid_sequences.append(seq)
        mid = _msg_id(message)
        in_prompt = bool((mid and mid in prompt_ids) or (not mid and seq is not None and seq in prompt_seqs))
        if boundary_seq is None or seq is None:
            boundary = "UNK"
        else:
            boundary = "POST" if seq >= boundary_seq else "PRE"
        rows.append({
            "ordinal": len(rows) + 1,
            "kind": kind,
            "msg_id": mid,
            "seq": seq,
            "in_prompt": in_prompt,
            "boundary": boundary,
            "text": text,
            "source_truncated": bool(source_truncated),
            "tool_name": str(tool_name or ""),
            "call_id": str(call_id or ""),
            "priority": 0,
            "preview": "",
        })

    for message in messages:
        role = str(message.get("role") or "")
        text, source_truncated = _message_text(message)
        seq = _as_int(message.get("event_seq"))
        boundary = "UNK" if boundary_seq is None or seq is None else ("POST" if seq >= boundary_seq else "PRE")
        special_kind = message.get("message_kind")

        if role == "user":
            mode = _control_mode(message, text)
            add_row(message, "CONTROL" if mode else "USER", text, source_truncated)
            if mode:
                rows[-1]["control_mode"] = mode
        elif role == "assistant":
            if _is_note(message, note_call_ids):
                add_row(message, "NOTE", text, source_truncated, call_id=message.get("tool_call_id", ""))
            elif text:
                add_row(message, "ASST", text, source_truncated)
            elif not _tool_calls(message):
                omitted_empty_assistant += 1
            for call in _tool_calls(message):
                name, call_id, arguments = _call_parts(call)
                call_text, call_truncated = _one_line(arguments)
                add_row(message, "CALL", call_text, call_truncated, tool_name=name, call_id=call_id)
        elif role == "tool":
            call_id = str(message.get("tool_call_id") or "")
            add_row(
                message,
                "RESULT",
                text,
                source_truncated,
                tool_name=message.get("name") or call_names.get(call_id, ""),
                call_id=call_id,
            )
        elif role == "system":
            looks_operational = bool(
                special_kind in {"provider_failure", "recovery_notice"}
                or boundary == "POST"
                or re.search(r"\b(error|failed|failure|interrupt|retry|unavailable|context limit)\b", text, re.I)
            )
            if looks_operational:
                add_row(message, "SYSTEM", text, source_truncated)
            else:
                excluded_system_messages += 1

    nonmonotonic = any(left > right for left, right in zip(valid_sequences, valid_sequences[1:]))
    latest_by_kind = {}
    for row in rows:
        latest_by_kind[row["kind"]] = row
    user_rows = [row for row in rows if row["kind"] == "USER"]
    latest_actionable = user_rows[-1] if user_rows else None
    post_users = [row for row in user_rows if row["boundary"] == "POST"]
    actionable_candidates = post_users or user_rows[-8:]
    controls = [row for row in rows if row["kind"] == "CONTROL"]
    latest_control = controls[-1] if controls else None

    important_kinds = {"USER", "CONTROL", "NOTE", "ASST", "SYSTEM"}
    row_total = max(1, len(rows))
    recent_cutoff = max(0, len(rows) - 100)
    latest_ordinals = {row["ordinal"] for row in latest_by_kind.values()}
    for row in rows:
        kind = row["kind"]
        priority = {
            "USER": 820,
            "CONTROL": 1_100,
            "NOTE": 800,
            "ASST": 720,
            "SYSTEM": 740,
            "CALL": 390,
            "RESULT": 450,
        }.get(kind, 300)
        if row is latest_actionable:
            priority = 2_200
        elif row is latest_control:
            priority = max(priority, 2_000)
        if row["boundary"] == "POST":
            priority += {
                "USER": 900,
                "NOTE": 850,
                "SYSTEM": 800,
                "RESULT": 720,
                "CALL": 680,
                "ASST": 650,
                "CONTROL": 700,
            }.get(kind, 500)
        elif row["in_prompt"] and kind != "SYSTEM":
            priority += 420
        if row["ordinal"] > recent_cutoff:
            priority += 360
        if row["ordinal"] in latest_ordinals:
            priority += 180
        if IMPORTANT_RE.search(row["text"]):
            priority += 500 if kind == "RESULT" else (230 if kind in {"SYSTEM", "NOTE", "ASST"} else 100)
        priority += int(100 * row["ordinal"] / row_total)
        row["priority"] = priority

    # Keep correlated calls/results at nearly the same priority. A cutoff can
    # still split a pair, so body_lines also marks omitted/missing counterparts.
    rows_by_call = {}
    for row in rows:
        if row["call_id"] and row["kind"] in {"CALL", "RESULT"}:
            rows_by_call.setdefault(row["call_id"], []).append(row)
    for correlated in rows_by_call.values():
        kinds = {row["kind"] for row in correlated}
        if kinds == {"CALL", "RESULT"}:
            pair_priority = max(row["priority"] for row in correlated)
            for row in correlated:
                row["priority"] = max(row["priority"], pair_priority - 1)

    thread_meta = context.get("thread") if isinstance(context.get("thread"), dict) else {}
    kind_counts = Counter(row["kind"] for row in rows)

    def row_label(row, preview=""):
        prompt = "P" if row["in_prompt"] else "N"
        seq = row["seq"] if row["seq"] is not None else "?"
        mid = _clip_field(row["msg_id"], 64) or "-"
        line = f"{row['ordinal']:05d} {prompt}/{row['boundary']:<4} {row['kind']:<7} seq={seq} msg={mid}"
        if row["tool_name"]:
            line += f" tool={_clip_field(row['tool_name'], 48)}"
        if row["call_id"]:
            line += f" call={_clip_field(row['call_id'], 52)}"
        if preview:
            line += " | " + preview
        return line

    def gap_line(gap_rows):
        counts = Counter(row["kind"] for row in gap_rows)
        first = gap_rows[0]["ordinal"]
        last = gap_rows[-1]["ordinal"]
        return f"..... omitted rows={len(gap_rows)} ordinals={first}-{last} kinds={_counter_text(counts)} ....."

    def body_lines(selected, with_previews):
        selected_ordinals = {row["ordinal"] for row in selected}
        selected_call_kinds = {}
        for row in selected:
            if row["call_id"] and row["kind"] in {"CALL", "RESULT"}:
                selected_call_kinds.setdefault(row["call_id"], set()).add(row["kind"])
        lines = []
        gap = []
        for row in rows:
            if row["ordinal"] not in selected_ordinals:
                gap.append(row)
                continue
            if gap:
                lines.append(gap_line(gap))
                gap = []
            line = row_label(row, row["preview"] if with_previews else "")
            if row["call_id"] and row["kind"] in {"CALL", "RESULT"}:
                counterpart = "RESULT" if row["kind"] == "CALL" else "CALL"
                all_kinds = {item["kind"] for item in rows_by_call.get(row["call_id"], [])}
                selected_kinds = selected_call_kinds.get(row["call_id"], set())
                if counterpart in all_kinds and counterpart not in selected_kinds:
                    line += f" [{counterpart} omitted]"
                elif counterpart not in all_kinds:
                    line += f" [{counterpart} missing]"
            lines.append(line)
        if gap:
            lines.append(gap_line(gap))
        return lines

    inspect_ids = []
    inspect_more = 0

    def latest_ref(kind):
        row = latest_by_kind.get(kind)
        if not row:
            return "-"
        return f"{_clip_field(row['msg_id'], 64) or '-'}@{row['seq'] if row['seq'] is not None else '?'}"

    def header_lines(selected):
        selected_set = {row["ordinal"] for row in selected}
        selected_counts = Counter(row["kind"] for row in selected)
        omitted_counts = Counter(row["kind"] for row in rows if row["ordinal"] not in selected_set)
        modes = [str(row.get("control_mode")) for row in controls if row.get("control_mode")]
        candidate_refs = [
            f"{_clip_field(row['msg_id'], 64) or '-'}@{row['seq'] if row['seq'] is not None else '?'}"
            for row in actionable_candidates[-8:]
        ]
        current_start = compaction.get("current_prompt_starts_at_msg_id") or "-"
        lines = [
            "THREAD NARRATIVE SKELETON FOR COMPACTION v2",
            (
                f"watermark_event_seq={thread_meta.get('loaded_through_event_seq', '?')} "
                f"loaded_at={_clip_field(thread_meta.get('loaded_at', '?'), 40)} "
                f"usable_messages={len(messages)} expanded_rows={len(rows)} selected_rows={len(selected)}"
            ),
            (
                f"row_counts={_counter_text(kind_counts)} selected={_counter_text(selected_counts)} "
                f"omitted={_counter_text(omitted_counts)} excluded_standing_system={excluded_system_messages} "
                f"omitted_empty_assistant={omitted_empty_assistant}"
            ),
            (
                f"compactions={len(context.get('compactions') or [])} "
                f"current_marker_seq={compaction.get('marker_event_seq', '-')} "
                f"current_start_seq={boundary_seq if boundary_seq is not None else '-'} "
                f"current_start_msg={_clip_field(current_start, 64)}"
            ),
            (
                f"control_mode={modes[-1] if modes else '-'} "
                f"control_msg={_clip_field(latest_control['msg_id'], 64) if latest_control else '-'} "
                f"latest_actionable_user={latest_ref('USER')}"
            ),
            "actionable_user_candidates=" + (", ".join(candidate_refs) if candidate_refs else "none"),
            (
                "latest_rows="
                f"NOTE:{latest_ref('NOTE')} ASST:{latest_ref('ASST')} "
                f"CALL:{latest_ref('CALL')} RESULT:{latest_ref('RESULT')} SYSTEM:{latest_ref('SYSTEM')}"
            ),
            "inspect_full_msg_ids=" + (", ".join(_clip_field(item, 64) for item in inspect_ids) if inspect_ids else "none"),
            f"inspect_full_additional={inspect_more}",
            "warnings=" + ("nonmonotonic_event_sequences" if nonmonotonic else "none"),
            (
                f"hard_limits=chars:{MAX_OUTPUT_CHARS},lines:{MAX_OUTPUT_LINES}; "
                "Legend P/N=in/not in current provider prompt; PRE/POST/UNK=relative to current compaction start."
            ),
            "Kinds USER=actionable user, CONTROL=compaction instruction, NOTE=Assistant Note, ASST=normal assistant, SYSTEM=operational system, CALL/RESULT=tool evidence.",
            "Rows are a prioritized chronological map, not proof. Inspect truncated/important rows by full msg_id before checkpointing claims.",
            "\n--- selected rows in canonical chronology ---",
        ]
        return lines

    ranked = sorted(rows, key=lambda row: (-row["priority"], -row["ordinal"]))
    selected_count = min(MAX_SELECTED_ROWS, len(ranked))

    def base_fits(count):
        chosen = sorted(ranked[:count], key=lambda row: row["ordinal"])
        lines = header_lines(chosen) + body_lines(chosen, False)
        text = "\n".join(lines).rstrip() + "\n"
        return len(text) <= int(MAX_OUTPUT_CHARS * 0.66) and len(text.splitlines()) <= MAX_OUTPUT_LINES

    while selected_count > 1 and not base_fits(selected_count):
        selected_count = max(1, selected_count - max(1, selected_count // 10))
    selected = sorted(ranked[:selected_count], key=lambda row: row["ordinal"])

    def render():
        lines = header_lines(selected) + body_lines(selected, True)
        return "\n".join(lines).rstrip() + "\n"

    base_output = render()
    # Reserve room for the inspection list, which is finalized only after
    # snippet allocation reveals which selected rows were actually truncated.
    remaining = MAX_OUTPUT_CHARS - len(base_output) - 700
    allocation_order = sorted(selected, key=lambda row: (-row["priority"], -row["ordinal"]))

    def desired_limit(row):
        kind = row["kind"]
        if row is latest_actionable:
            return 1_800
        if row["boundary"] == "POST":
            return {
                "USER": 1_400,
                "NOTE": 1_200,
                "SYSTEM": 1_100,
                "RESULT": 1_000,
                "ASST": 900,
                "CALL": 800,
                "CONTROL": 320,
            }.get(kind, 600)
        if row["in_prompt"]:
            return 700 if kind in important_kinds else 520
        return 260 if kind in important_kinds else 180

    def allocate(target_for):
        nonlocal remaining
        for row in allocation_order:
            if remaining <= 3:
                return
            target = min(len(row["text"]), max(0, int(target_for(row))))
            if target <= len(row["preview"]):
                continue
            old = row["preview"]
            overhead = 3 if not old and row["text"] else 0
            affordable = len(old) + max(0, remaining - overhead)
            target = min(target, affordable)
            if target <= len(old):
                continue
            new = _preview(
                row["text"],
                target,
                tail_worthy=row["kind"] in {"NOTE", "ASST", "SYSTEM", "RESULT"},
            )
            delta = len(new) - len(old) + overhead
            if delta <= remaining:
                row["preview"] = new
                remaining -= delta

    allocate(lambda row: min(desired_limit(row), 600) if row["priority"] >= 1_400 else 0)
    allocate(lambda row: min(desired_limit(row), 90))
    allocate(desired_limit)

    inspect_candidates = [
        row
        for row in sorted(selected, key=lambda item: (-item["priority"], -item["ordinal"]))
        if row["msg_id"]
        and (row["source_truncated"] or len(row["preview"]) < len(row["text"]))
    ]
    for row in inspect_candidates:
        if row["msg_id"] not in inspect_ids:
            inspect_ids.append(row["msg_id"])
        if len(inspect_ids) >= 8:
            break
    inspect_more = len({row["msg_id"] for row in inspect_candidates}) - len(inspect_ids)

    output = render()
    if len(output) > MAX_OUTPUT_CHARS or len(output.splitlines()) > MAX_OUTPUT_LINES:
        raise AssertionError(
            f"compaction skeleton exceeded hard bound: chars={len(output)} lines={len(output.splitlines())}"
        )
    sys.stdout.write(output)


_main()
```

### Optional: extract the script into an artifact or file

The `skill` output has stable source coordinates. To extract the embedded script without copying a long preview:

1. Load this skill with `line_numbers=true`.
2. Read the displayed opening and closing fence line numbers.
3. Call `extract_tool_output` on the skill call, using a 1-based half-open range `[start_line, end_line)`. Select the Python body only: start immediately after `````python`` and end at the closing ````` line.
4. Save the returned provider artifact with `save_provider_artifact_to_file` if an inspectable/copyable file is useful.

For this version of the skill, the body occupies displayed lines 41 through 602. (`skill` adds its two-line `# Skill:` wrapper to the document coordinates.)

```text
skill(name="compaction-checkpoint", line_numbers=true)
extract_tool_output(start_line=41, end_line=603, filename="compaction_narrative_skeleton.py")
save_provider_artifact_to_file(artifact_id="<receipt artifact id>", path=".scratch/compaction_narrative_skeleton.py")
```

Omitting `source_tool_call_id` selects the immediately preceding eligible published tool output; pass the skill call ID explicitly if another tool call intervened. Displayed line-number prefixes are presentation only and are never written into extracted bytes. Always derive the range from the current numbered skill output; do not rely on the illustrative numbers above after editing this document.

Extraction does not change the execution contract. Do not run the saved file as a standalone shell/Python script. Execute its contents through the hydrated `python_repl` (for example, by passing the extracted text as the REPL's code) so the thread-context globals and helpers are present.

### Required follow-up inspection

After the first pass:

1. Identify `latest_actionable_user` and all plausible `actionable_user_candidates`; never treat `CONTROL` as the task.
2. Inspect every truncated or decision-critical row that supports the active task, a test/command result, a design decision, or the latest stable tool state. Start with `inspect_full_msg_ids` and use `get_message(...)` / `print_message(...)`.
3. Correlate `CALL` with `RESULT`. A call proves intent, not success. Never claim a test passed, a file changed, or a commit exists without a result or independent verification.
4. Treat `NOTE` rows as reported progress, not automatically verified fact. Preserve manager/worker Assistant Notes because they may contain the only durable handoff.
5. Scan relevant older `PRE` rows for accepted/rejected designs, earlier failures, commits, and constraints. Omission markers mean the interval was not shown; use `search_thread(...)`, `messages_by_id`, or the sanitized `context_files` caches when needed.
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

