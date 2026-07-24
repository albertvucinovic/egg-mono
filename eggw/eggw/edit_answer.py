from __future__ import annotations

"""EggW edit-answer and general editor draft preparation service."""

from typing import Any

from eggthreads.edit_answer import EditAnswerDraft, prepare_edit_answer_draft
from eggthreads.editor_sources import read_editor_draft_file, resolve_editor_source
from eggthreads import get_thread_working_directory

from .editor_files import read_editor_file, register_editor_file
from .models import EditAnswerDraftResponse

EDIT_ANSWER_MODAL_ACTION = "open_edit_answer_modal"


def _model_dump(model: Any) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        payload = model.model_dump(exclude_none=True)
    else:
        payload = model.dict(exclude_none=True)
    return payload


def _resolve_selector(*, selector: str | None = None, source_msg_id: str | None = None) -> str:
    wanted = str(selector or "").strip()
    source = str(source_msg_id or "").strip()
    if wanted and source:
        raise ValueError("Provide either selector or source_msg_id, not both.")
    return source or wanted


def _prepared_message(source_label: str, source_suffix: str, draft_text: str) -> str:
    suffix = f" {source_suffix}" if source_suffix else ""
    label = source_label or "assistant answer"
    if label == "input message":
        return "Prepared input message draft." if str(draft_text or "").strip() else "Prepared empty input message draft."
    if label.endswith(" message"):
        return f"Prepared editable {label}{suffix}."
    return f"Prepared quoted {label}{suffix}."


def _response_from_draft(draft: EditAnswerDraft) -> EditAnswerDraftResponse:
    return EditAnswerDraftResponse(
        action=EDIT_ANSWER_MODAL_ACTION,
        draft=draft.draft,
        source_msg_id=draft.source_msg_id,
        source_kind=draft.source_kind,
        source_suffix=draft.source_suffix,
        source_label=draft.source_label,
        suppress_transcript=True,
        message=_prepared_message(draft.source_label, draft.source_suffix, draft.draft),
    )


def prepare_edit_answer_draft_response(
    db: Any,
    thread_id: str,
    *,
    selector: str | None = None,
    source_msg_id: str | None = None,
    fallback_to_empty_input: bool = False,
    fallback_unmatched_selector_to_input: bool = False,
) -> EditAnswerDraftResponse:
    """Prepare an EggW edit-answer draft using shared transcript selection."""

    resolved_selector = _resolve_selector(selector=selector, source_msg_id=source_msg_id)
    draft = prepare_edit_answer_draft(
        db,
        thread_id,
        resolved_selector,
        prefer_waiting_note=True,
        fallback_to_empty_input=fallback_to_empty_input,
        fallback_unmatched_selector_to_input=fallback_unmatched_selector_to_input,
    )
    exact_source = str(source_msg_id or "").strip()
    if exact_source and draft.source_msg_id != exact_source:
        raise ValueError(f"No assistant answer matched source_msg_id {exact_source!r}.")
    return _response_from_draft(draft)


def prepare_editor_response(db: Any, thread_id: str, arg: str) -> EditAnswerDraftResponse:
    """Prepare shared ``/editor`` source semantics for the browser modal."""

    source = resolve_editor_source(db, thread_id, arg, get_thread_working_directory(db, thread_id))
    if source.mode == "ambiguous":
        return EditAnswerDraftResponse(
            action="request_completion",
            draft="",
            source_msg_id="",
            source_kind="input_message",
            source_label="input message",
            message=source.resolution.message if source.resolution else "Ambiguous editor source.",
            suppress_transcript=True,
        )
    if source.mode == "file" and source.path is not None:
        try:
            text, fingerprint, exists = read_editor_file(source.path)
        except RuntimeError as exc:
            raise ValueError(str(exc)) from exc
        handle = register_editor_file(
            thread_id,
            source.path,
            fingerprint=fingerprint,
            existed=exists,
        )
        return EditAnswerDraftResponse(
            draft=text,
            source_msg_id="",
            source_kind="message",
            source_suffix=source.path.name,
            source_label="file",
            message=f"Opened {source.path} in the browser editor.",
            editor_mode="file",
            file_path=str(source.path),
            file_handle=handle,
        )

    text = source.draft
    if source.mode == "file_draft" and source.path is not None:
        text = read_editor_draft_file(source.path)
    draft = EditAnswerDraft(
        draft=text,
        source_msg_id=source.record_id,
        source_kind="message" if source.mode in {"record", "file_draft"} else "input_message",
        source_suffix=source.source_suffix,
        source_label=source.source_label,
    )
    return _response_from_draft(draft)


def edit_answer_draft_response_data(response: EditAnswerDraftResponse) -> dict[str, Any]:
    """Return the command payload for one prepared browser editor response."""

    return _model_dump(response)


__all__ = [
    "EDIT_ANSWER_MODAL_ACTION",
    "edit_answer_draft_response_data",
    "prepare_edit_answer_draft_response",
    "prepare_editor_response",
]
