from pathlib import Path

from eggthreads.quick_start import parse_quick_start_args, quick_start_args_from_json


def test_text_arguments_become_one_unsent_draft_with_quoted_whitespace_preserved(tmp_path: Path):
    request = parse_quick_start_args(["Tell", "me a story", "about eggs"], cwd=tmp_path)

    assert request is not None
    assert request.kind == "draft"
    assert request.draft == "Tell me a story about eggs"
    assert request.source_path is None


def test_sole_existing_file_becomes_attachment_request(tmp_path: Path):
    source = tmp_path / "notes with spaces.txt"
    source.write_text("do not inline me", encoding="utf-8")

    request = parse_quick_start_args([source.name], cwd=tmp_path)

    assert request is not None
    assert request.kind == "attachment"
    assert request.draft == ""
    assert request.source_path == source.resolve()


def test_existing_file_among_multiple_args_remains_text(tmp_path: Path):
    source = tmp_path / "notes.txt"
    source.write_text("content", encoding="utf-8")

    request = parse_quick_start_args(["Review", source.name], cwd=tmp_path)

    assert request is not None
    assert request.kind == "draft"
    assert request.draft == "Review notes.txt"


def test_missing_or_directory_argument_is_text(tmp_path: Path):
    assert parse_quick_start_args(["missing.txt"], cwd=tmp_path).kind == "draft"
    assert parse_quick_start_args([str(tmp_path)], cwd=tmp_path).kind == "draft"


def test_unknown_tilde_user_argument_is_text_instead_of_crashing(tmp_path: Path):
    raw = "~egg-user-that-must-not-exist-7f31c9/file.txt"

    request = parse_quick_start_args([raw], cwd=tmp_path)

    assert request is not None
    assert request.kind == "draft"
    assert request.draft == raw


def test_json_argv_decoder_rejects_invalid_or_non_string_payloads():
    assert quick_start_args_from_json('["Tell", "me a story"]') == ["Tell", "me a story"]
    assert quick_start_args_from_json('{"draft": "no"}') == []
    assert quick_start_args_from_json('["ok", 3]') == []
    assert quick_start_args_from_json('broken') == []
    assert quick_start_args_from_json(None) == []
