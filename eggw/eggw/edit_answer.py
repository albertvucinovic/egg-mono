from __future__ import annotations

"""EggW edit-answer draft preparation service."""

from typing import Any

from eggthreads.edit_answer import prepare_edit_answer_draft

from .models import EditAnswerDraftResponse


EDIT_ANSWER_MODAL_ACTION = "open_edit_answer_modal"


def _model_dump(model: Any) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()


def _resolve_selector(*, selector: str | None = None, source_msg_id: str | None = None) -> str:
    wanted = str(selector or "").strip()
    source = str(source_msg_id or "").strip()
    if wanted and source:
        raise ValueError("Provide either selector or source_msg_id, not both.")
    return source or wanted


def _prepared_message(source_label: str, source_suffix: str) -> str:
    suffix = f" {source_suffix}" if source_suffix else ""
    label = source_label or "assistant answer"
    return f"Prepared quoted {label}{suffix}."


def prepare_edit_answer_draft_response(
    db: Any,
    thread_id: str,
    *,
    selector: str | None = None,
    source_msg_id: str | None = None,
) -> EditAnswerDraftResponse:
    """Prepare an EggW edit-answer draft response using shared eggthreads logic."""

    resolved_selector = _resolve_selector(selector=selector, source_msg_id=source_msg_id)
    draft = prepare_edit_answer_draft(db, thread_id, resolved_selector, prefer_waiting_note=True)

    exact_source = str(source_msg_id or "").strip()
    if exact_source and draft.source_msg_id != exact_source:
        raise ValueError(f"No assistant answer matched source_msg_id {exact_source!r}.")

    message = _prepared_message(draft.source_label, draft.source_suffix)
    return EditAnswerDraftResponse(
        action=EDIT_ANSWER_MODAL_ACTION,
        draft=draft.draft,
        source_msg_id=draft.source_msg_id,
        source_kind=draft.source_kind,
        source_suffix=draft.source_suffix,
        source_label=draft.source_label,
        suppress_transcript=True,
        message=message,
    )


def edit_answer_draft_response_data(response: EditAnswerDraftResponse) -> dict[str, Any]:
    """Return the command data payload for an edit-answer draft response."""

    return _model_dump(response)


__all__ = [
    "EDIT_ANSWER_MODAL_ACTION",
    "edit_answer_draft_response_data",
    "prepare_edit_answer_draft_response",
]
