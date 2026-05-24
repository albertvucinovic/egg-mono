# Compaction Checkpoint Skill

Use this skill when the provider context starts immediately after an Egg thread compaction and the prompt asks for a continuation checkpoint.

## Goal

Create a concise durable checkpoint of the work state, then choose whether to stop or continue based on the compaction mode.

The checkpoint should preserve:

- the pending user request or task;
- important decisions, invariants, and design constraints;
- files changed or intended to change;
- commands/tests already run and their results;
- known failures, risks, or unresolved questions;
- exact next steps.

Use hydrated thread-history helpers when needed (`all_messages`, `current_prompt_messages`, `older_messages_not_in_prompt`, `messages_by_id`, `search_thread(...)`, `get_message(...)`, `print_message(...)`, `reload_thread_context()`).

## First-pass narrative skeleton

Before writing the checkpoint, first run this `python_repl` script. It prints a compact chronological skeleton of the visible user messages, contentful LLM assistant messages, and Assistant Notes / interim answers. Treat this tool output as the starting map for the compaction summary, then do targeted follow-up inspection with `get_message(...)`, `print_message(...)`, `search_thread(...)`, or raw DB/file inspection as needed.

Do not summarize from only the current prompt tail. Use this skeleton to notice older relevant work, repeated themes, and the exact latest user request.

```python
import json
import re
from collections import Counter

MAX_OUTPUT_CHARS = 60000
OLDER_SNIPPET_STEPS = [70, 50, 35, 20, 0]
DETAIL_SNIPPET_STEPS = [300, 220, 160, 120, 80, 50, 30, 0]
RECENT_DETAIL_COUNT = 90


def msg_id(m):
    return str(m.get("msg_id") or m.get("id") or "")


def event_seq(m):
    try:
        return int(m.get("event_seq"))
    except Exception:
        return None


def suffix(value, n=8):
    text = str(value or "")
    return text[-n:] if text else "-"


def clean_text(value, limit):
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except Exception:
            text = repr(value)
    text = re.sub(r"\s+", " ", text).strip()
    if limit <= 0:
        return ""
    if len(text) > limit:
        text = text[: max(0, limit - 1)].rstrip() + "…"
    return text


def is_assistant_note(m):
    """Best-effort detection of answer_user_while_preserving_llm_turn notes.

    Raw DB payloads have answer_user_preserve_turn=True. Hydrated context may
    omit that flag, but usually preserves the note's tool_call_id. Normal final
    assistant messages should not have tool_call_id.
    """
    if not isinstance(m, dict) or m.get("role") != "assistant":
        return False
    if m.get("answer_user_preserve_turn"):
        return True
    return bool(m.get("tool_call_id") and m.get("content") and not m.get("tool_calls"))


def is_llm_assistant_message(m):
    """Contentful assistant message, excluding Assistant Notes and tool-call-only turns."""
    return (
        isinstance(m, dict)
        and m.get("role") == "assistant"
        and not is_assistant_note(m)
        and bool(str(m.get("content") or "").strip())
    )


def selected_kind(m):
    if not isinstance(m, dict):
        return None
    if m.get("role") == "user":
        return "USER"
    if is_assistant_note(m):
        return "NOTE"
    if is_llm_assistant_message(m):
        return "ASST"
    return None


def current_prompt_keys():
    mids, seqs = set(), set()
    for m in globals().get("current_prompt_messages", []) or []:
        if not isinstance(m, dict):
            continue
        mid = msg_id(m)
        if mid:
            mids.add(mid)
        seq = event_seq(m)
        if seq is not None:
            seqs.add(seq)
    return mids, seqs


def current_compaction_start_seq():
    current = [
        c for c in (globals().get("compactions", []) or [])
        if isinstance(c, dict) and c.get("is_current")
    ]
    if not current:
        return None
    try:
        return int(current[-1].get("current_prompt_starts_at_event_seq"))
    except Exception:
        return None


def scope_marker(m, current_ids, current_seqs, current_start_seq):
    mid = msg_id(m)
    seq = event_seq(m)
    if (mid and mid in current_ids) or (seq is not None and seq in current_seqs):
        return "P"  # in current provider prompt
    if current_start_seq is not None and seq is not None and seq >= current_start_seq:
        return "C"  # after current compaction start, but provider-hidden/not in prompt
    return "O"      # older visible history


def needs_detail(index0, m, selected_len, current_ids, current_seqs, current_start_seq):
    seq = event_seq(m)
    return (
        scope_marker(m, current_ids, current_seqs, current_start_seq) in {"P", "C"}
        or index0 >= max(0, selected_len - RECENT_DETAIL_COUNT)
        or (current_start_seq is not None and seq is not None and seq >= current_start_seq)
    )


def format_line(n, m, snippet_limit, current_ids, current_seqs, current_start_seq, *, detail):
    kind = selected_kind(m) or "?"
    scope = scope_marker(m, current_ids, current_seqs, current_start_seq)
    id_part = f"msg_id={msg_id(m)}" if detail else f"msg={suffix(msg_id(m))}"
    base = f"{n:04d} {scope} {kind:<4} seq={m.get('event_seq', '?')} {id_part}"
    ts = str(m.get("ts") or "")[:19]
    if ts:
        base += f" ts={ts}"
    if kind == "NOTE" and m.get("tool_call_id"):
        base += f" note_call={suffix(m.get('tool_call_id'), 10)}"
    text = clean_text(m.get("content"), snippet_limit)
    return base + (f" | {text}" if text else "")


def render(selected, older_limit, detail_limit):
    current_ids, current_seqs = current_prompt_keys()
    current_start_seq = current_compaction_start_seq()
    total = len(selected)
    lines = []
    for i, m in enumerate(selected):
        detail = needs_detail(i, m, total, current_ids, current_seqs, current_start_seq)
        limit = detail_limit if detail else older_limit
        lines.append(
            format_line(
                i + 1,
                m,
                limit,
                current_ids,
                current_seqs,
                current_start_seq,
                detail=detail,
            )
        )
    return "\n".join(lines)


messages = [m for m in (globals().get("all_messages", []) or []) if isinstance(m, dict)]
selected = [m for m in messages if selected_kind(m)]

role_counts = Counter(str(m.get("role") or "?") for m in messages)
kind_counts = Counter(selected_kind(m) for m in selected)

omitted_tool_call_only_assistant = sum(
    1
    for m in messages
    if m.get("role") == "assistant"
    and not selected_kind(m)
    and m.get("tool_calls")
)

candidates = []
for older_limit in OLDER_SNIPPET_STEPS:
    for detail_limit in DETAIL_SNIPPET_STEPS:
        body = render(selected, older_limit, detail_limit)
        if len(body) <= MAX_OUTPUT_CHARS:
            # Prefer:
            # 1. keeping some older snippets if possible;
            # 2. richer current/recent detail;
            # 3. richer older snippets as a tie-breaker.
            score = (1 if older_limit > 0 else 0, detail_limit, older_limit)
            candidates.append((score, older_limit, detail_limit, body))

if candidates:
    _score, older_limit, detail_limit, body = max(candidates, key=lambda x: x[0])
else:
    older_limit = detail_limit = 0
    body = render(selected, 0, 0)

current = [
    c for c in (globals().get("compactions", []) or [])
    if isinstance(c, dict) and c.get("is_current")
]
cur = current[-1] if current else {}

print("THREAD NARRATIVE SKELETON FOR COMPACTION")
print(
    f"visible_messages={len(messages)} selected={len(selected)} output_chars={len(body)} "
    f"older_snippet_chars={older_limit} detail_snippet_chars={detail_limit} "
    f"recent_detail_count={RECENT_DETAIL_COUNT}"
)
print("selected_counts=" + ", ".join(f"{k}={kind_counts[k]}" for k in sorted(kind_counts)))
print("visible_role_counts=" + ", ".join(f"{k}={role_counts[k]}" for k in sorted(role_counts)))
print(f"omitted_tool_call_only_assistant={omitted_tool_call_only_assistant}")
if globals().get("compactions"):
    print(
        f"compactions={len(compactions)} "
        f"current_start_seq={cur.get('current_prompt_starts_at_event_seq')} "
        f"current_start_msg={suffix(cur.get('current_prompt_starts_at_msg_id'))}"
    )
print("Legend: P=in current provider prompt, C=after current compaction start but provider-hidden/not in prompt, O=older visible history.")
print("Kinds: USER=user message, ASST=contentful LLM assistant message, NOTE=Assistant Note/interim answer.")
print("Use msg/seq identifiers for targeted follow-up with get_message(), print_message(), search_thread(), or raw DB inspection.")
print("\n--- skeleton ---")
print(body)
```

After this first pass:

- identify the latest actionable user request(s), especially entries marked `P` or `C`;
- scan older `O` entries for relevant prior design decisions, failed attempts, commits, and constraints;
- inspect any important truncated entry by full `msg_id` or `event_seq` before relying on it;
- only then write the checkpoint.

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

## Output style

Keep the checkpoint concise but specific. Prefer bullets. Avoid replaying the whole transcript.
