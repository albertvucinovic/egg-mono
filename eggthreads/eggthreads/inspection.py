from __future__ import annotations

"""Shared, read-only transcript record inspection for ``/show``.

The resolver operates on the canonical effective message projection for exactly
one selected thread.  It never scans ancestors, siblings, or descendants
implicitly: selecting another thread/view is the existing access boundary.
"""

import copy
import json
from dataclasses import dataclass, replace
from typing import Any, Literal, Mapping, Sequence

from .content_parts import content_to_plain_text
from .projection import load_thread_projection

ShowRecordKind = Literal["message", "assistant_note", "tool_declaration", "tool_result"]
ShowResolutionStatus = Literal["selected", "ambiguous", "missing"]

SHOW_AMBIGUOUS_LIMIT = 10
SHOW_COMPLETION_LIMIT = 20
SHOW_PREVIEW_CHARS = 80


@dataclass(frozen=True)
class ShowRecordCandidate:
    """One canonically identified, currently effective transcript record."""

    record_id: str
    kind: ShowRecordKind
    thread_id: str
    message_id: str
    event_seq: int
    order: int
    label: str
    preview: str
    message: Mapping[str, Any]
    tool_call_id: str = ""
    tool_call: Mapping[str, Any] | None = None
    paired_message_ids: tuple[str, ...] = ()

    def summary(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "kind": self.kind,
            "thread_id": self.thread_id,
            "message_id": self.message_id,
            "tool_call_id": self.tool_call_id or None,
            "event_seq": self.event_seq,
            "label": self.label,
            "preview": self.preview,
            "paired_message_ids": list(self.paired_message_ids),
        }


@dataclass(frozen=True)
class ShowRecordResolution:
    """Deterministic result of resolving one case-sensitive ID hint."""

    status: ShowResolutionStatus
    thread_id: str
    hint: str
    watermark_event_seq: int
    selected: ShowRecordCandidate | None = None
    candidates: tuple[ShowRecordCandidate, ...] = ()
    total_matches: int = 0
    message: str = ""


def _one_line(value: Any, *, limit: int = SHOW_PREVIEW_CHARS) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def _tool_call_id(tool_call: Mapping[str, Any]) -> str:
    return str(tool_call.get("id") or tool_call.get("tool_call_id") or "")


def _tool_call_parts(tool_call: Mapping[str, Any]) -> tuple[str, Any]:
    function = tool_call.get("function")
    function = function if isinstance(function, Mapping) else {}
    name = str(function.get("name") or tool_call.get("name") or "tool")
    arguments = function.get("arguments", tool_call.get("arguments", ""))
    return name, arguments


def _tool_call_preview(tool_call: Mapping[str, Any]) -> str:
    name, arguments = _tool_call_parts(tool_call)
    if isinstance(arguments, (dict, list)):
        try:
            arguments = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
        except Exception:
            arguments = str(arguments)
    body = _one_line(arguments)
    return f"{name}({body})" if body else name


def _message_kind(message: Mapping[str, Any]) -> ShowRecordKind:
    if message.get("role") == "tool":
        return "tool_result"
    if message.get("role") == "assistant" and message.get("answer_user_preserve_turn"):
        return "assistant_note"
    return "message"


def _message_label(message: Mapping[str, Any], kind: ShowRecordKind) -> str:
    if kind == "tool_result":
        name = str(message.get("name") or "tool")
        return f"Tool result: {name}"
    if kind == "assistant_note":
        return "Assistant Note"
    role = str(message.get("role") or "message")
    return role.replace("_", " ").title()


def _message_preview(message: Mapping[str, Any]) -> str:
    return _one_line(content_to_plain_text(message.get("content")))


def _candidate_sort_key(candidate: ShowRecordCandidate) -> tuple[int, int, str, str]:
    return (-candidate.event_seq, -candidate.order, candidate.kind, candidate.record_id)


def _effective_candidates(db: Any, thread_id: str) -> tuple[list[ShowRecordCandidate], int]:
    normalized_thread = str(thread_id or "").strip()
    if db is None or not normalized_thread or db.get_thread(normalized_thread) is None:
        return [], -1

    watermark = int(db.max_event_seq(normalized_thread))
    projection = load_thread_projection(db, normalized_thread, watermark)
    candidates: list[ShowRecordCandidate] = []
    order = 0

    for state in projection.messages:
        message = state.as_message_dict()
        # Projected ``event:<seq>`` identities exist only to make legacy
        # identity-less messages reducible; ``as_message_dict`` deliberately
        # hides them. /show must never turn that internal key into a public ID.
        msg_id = str(message.get("msg_id") or "")
        if not msg_id:
            continue
        kind = _message_kind(message)
        candidates.append(
            ShowRecordCandidate(
                record_id=msg_id,
                kind=kind,
                thread_id=normalized_thread,
                message_id=msg_id,
                event_seq=int(state.created_event_seq),
                order=order,
                label=_message_label(message, kind),
                preview=_message_preview(message),
                message=message,
                tool_call_id=(str(message.get("tool_call_id") or "") if kind == "tool_result" else ""),
            )
        )
        order += 1

        # A durable assistant declaration has its own provider/shared identity.
        # User-originated tool calls remain inspectable through their message ID;
        # do not silently broaden the declared candidate kind.
        if message.get("role") != "assistant":
            continue
        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        for raw_tool_call in tool_calls:
            if not isinstance(raw_tool_call, Mapping):
                continue
            call_id = _tool_call_id(raw_tool_call)
            if not call_id:
                continue
            name, _arguments = _tool_call_parts(raw_tool_call)
            candidates.append(
                ShowRecordCandidate(
                    record_id=call_id,
                    kind="tool_declaration",
                    thread_id=normalized_thread,
                    message_id=msg_id,
                    event_seq=int(state.created_event_seq),
                    order=order,
                    label=f"Tool declaration: {name}",
                    preview=_tool_call_preview(raw_tool_call),
                    message=message,
                    tool_call_id=call_id,
                    tool_call=copy.deepcopy(dict(raw_tool_call)),
                )
            )
            order += 1

    result_messages: dict[str, list[str]] = {}
    declaration_messages: dict[str, list[str]] = {}
    for candidate in candidates:
        if candidate.kind == "tool_result" and candidate.tool_call_id:
            result_messages.setdefault(candidate.tool_call_id, []).append(candidate.message_id)
        elif candidate.kind == "tool_declaration" and candidate.tool_call_id:
            declaration_messages.setdefault(candidate.tool_call_id, []).append(candidate.message_id)

    paired: list[ShowRecordCandidate] = []
    for candidate in candidates:
        message_ids: Sequence[str] = ()
        if candidate.kind == "tool_declaration":
            message_ids = result_messages.get(candidate.tool_call_id, ())
        elif candidate.kind == "tool_result":
            message_ids = declaration_messages.get(candidate.tool_call_id, ())
        paired.append(replace(candidate, paired_message_ids=tuple(message_ids)))
    paired.sort(key=_candidate_sort_key)
    return paired, watermark


def list_show_record_candidates(db: Any, thread_id: str) -> list[ShowRecordCandidate]:
    """Return current-thread inspectable candidates, newest first."""

    candidates, _watermark = _effective_candidates(db, thread_id)
    return candidates


def _matches_hint(candidate: ShowRecordCandidate, hint: str) -> bool:
    return candidate.record_id.startswith(hint) or candidate.record_id.endswith(hint)


def _candidate_line(candidate: ShowRecordCandidate) -> str:
    preview = f" — {candidate.preview}" if candidate.preview else ""
    source = f" (message {candidate.message_id})" if candidate.kind == "tool_declaration" else ""
    return f"  {candidate.record_id} [{candidate.label}]{source}{preview}"


def resolve_show_record(db: Any, thread_id: str, hint: str) -> ShowRecordResolution:
    """Resolve an exact ID, then a unique case-sensitive prefix/suffix hint.

    Resolution is deliberately current-thread only.  Exact full identity wins
    over every fuzzy match.  Deleted and continue-skipped messages are absent
    from the effective projection and therefore fail with the same non-leaking
    missing result as every other inaccessible identity.
    """

    normalized_thread = str(thread_id or "").strip()
    wanted = str(hint or "").strip()
    candidates, watermark = _effective_candidates(db, normalized_thread)
    if not wanted:
        return ShowRecordResolution(
            status="missing",
            thread_id=normalized_thread,
            hint=wanted,
            watermark_event_seq=watermark,
            message="Usage: /show <id_hint>",
        )

    exact = [candidate for candidate in candidates if candidate.record_id == wanted]
    matches = exact if exact else [candidate for candidate in candidates if _matches_hint(candidate, wanted)]
    if len(matches) == 1:
        selected = matches[0]
        return ShowRecordResolution(
            status="selected",
            thread_id=normalized_thread,
            hint=wanted,
            watermark_event_seq=watermark,
            selected=selected,
            candidates=(selected,),
            total_matches=1,
            message=f"Showing {selected.label} {selected.record_id}.",
        )
    if matches:
        bounded = tuple(matches[:SHOW_AMBIGUOUS_LIMIT])
        omitted = len(matches) - len(bounded)
        suffix = f"\n  … and {omitted} more" if omitted else ""
        lines = "\n".join(_candidate_line(candidate) for candidate in bounded)
        return ShowRecordResolution(
            status="ambiguous",
            thread_id=normalized_thread,
            hint=wanted,
            watermark_event_seq=watermark,
            candidates=bounded,
            total_matches=len(matches),
            message=(
                f"/show hint {wanted!r} matched {len(matches)} inspectable records in the current thread; "
                f"use a full or longer ID:\n{lines}{suffix}"
            ),
        )
    return ShowRecordResolution(
        status="missing",
        thread_id=normalized_thread,
        hint=wanted,
        watermark_event_seq=watermark,
        message=f"No inspectable record matched {wanted!r} in the current thread.",
    )


def _message_token_stats(message: Mapping[str, Any]) -> dict[str, int]:
    try:
        from .token_count import snapshot_token_stats

        stats = snapshot_token_stats({"messages": [dict(message)]})
        per_message = stats.get("per_message") if isinstance(stats, Mapping) else None
        info = per_message.get(str(message.get("msg_id") or ""), {}) if isinstance(per_message, Mapping) else {}
    except Exception:
        info = {}
    return {
        "content_tokens": int(info.get("content_tokens") or 0),
        "reasoning_tokens": int(info.get("reasoning_tokens") or 0),
        "tool_calls_tokens": int(info.get("tool_calls_tokens") or 0),
        "total_tokens": int(info.get("total_tokens") or 0),
    }


def _public_message(candidate: ShowRecordCandidate) -> dict[str, Any]:
    source = candidate.message
    out: dict[str, Any] = {
        "id": candidate.message_id,
        "role": str(source.get("role") or ""),
        "content": copy.deepcopy(source.get("content")),
        "content_text": content_to_plain_text(source.get("content")),
        "timestamp": source.get("ts"),
        "event_seq": candidate.event_seq,
    }
    reasoning = source.get("reasoning")
    if not isinstance(reasoning, str):
        reasoning = source.get("reasoning_content")
    if isinstance(reasoning, str):
        out["reasoning"] = reasoning
    for key in (
        "tool_calls",
        "tool_stream",
        "tool_calls_stream",
        "tool_call_id",
        "output_optimizer",
        "name",
        "model_key",
        "tps",
        "answer_user_preserve_turn",
        "consumed_by_tool_call_id",
        "consumed_by_tool_name",
        "origin",
        "from_thread_id",
        "recovery_notice",
        "runner_error",
        "incomplete",
        "incomplete_reason",
        "user_tool_call",
    ):
        if key in source:
            out[key] = copy.deepcopy(source.get(key))
    token_stats = _message_token_stats(source)
    out["tokens"] = token_stats["total_tokens"] or None
    out["token_stats"] = token_stats
    return out


def show_record_target(candidate: ShowRecordCandidate, *, watermark_event_seq: int) -> dict[str, Any]:
    """Return the bounded public command result consumed by both clients."""

    target = candidate.summary()
    target.update(
        {
            "watermark_event_seq": int(watermark_event_seq),
            "message": _public_message(candidate),
            "tool_call": copy.deepcopy(dict(candidate.tool_call)) if candidate.tool_call is not None else None,
        }
    )
    return target


def show_record_completion_items(
    db: Any,
    thread_id: str,
    arg: str = "",
    *,
    limit: int = SHOW_COMPLETION_LIMIT,
) -> list[dict[str, Any]]:
    """Return bounded catalog completion items for the final ``/show`` token."""

    text = str(arg or "")
    fragment = text.split()[-1] if text.split() else ""
    candidates, _watermark = _effective_candidates(db, thread_id)
    if fragment:
        exact = [candidate for candidate in candidates if candidate.record_id == fragment]
        candidates = exact or [candidate for candidate in candidates if _matches_hint(candidate, fragment)]
    items: list[dict[str, Any]] = []
    for candidate in candidates[: max(0, int(limit))]:
        short_id = candidate.record_id[-8:] if len(candidate.record_id) > 8 else candidate.record_id
        preview = f" · {candidate.preview}" if candidate.preview else ""
        items.append(
            {
                "display": f"[{short_id}] {candidate.label}{preview}",
                "insert": candidate.record_id,
                "replace": len(fragment),
                "meta": f"{candidate.kind} · {candidate.record_id}",
            }
        )
    return items


__all__ = [
    "SHOW_AMBIGUOUS_LIMIT",
    "SHOW_COMPLETION_LIMIT",
    "ShowRecordCandidate",
    "ShowRecordResolution",
    "list_show_record_candidates",
    "resolve_show_record",
    "show_record_completion_items",
    "show_record_target",
]
