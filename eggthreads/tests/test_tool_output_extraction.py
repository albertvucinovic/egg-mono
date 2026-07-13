from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

import eggthreads as ts
from eggthreads.output_policy import OutputPolicyRequest
from eggthreads.provider_output_artifacts import resolve_provider_output_bytes
from eggthreads.runner import stash_tool_output_and_build_preview
from eggthreads.tools import ToolRegistry


def _make_db(tmp_path: Path) -> ts.ThreadsDB:
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    return db


def _declare(
    db: ts.ThreadsDB,
    thread_id: str,
    tool_call_id: str,
    name: str,
    *,
    arguments: dict | None = None,
    role: str = "assistant",
    msg_id: str | None = None,
    extra_calls: list[dict] | None = None,
) -> int:
    calls = [
        {
            "id": tool_call_id,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments or {})},
        }
    ]
    calls.extend(extra_calls or [])
    return db.append_event(
        f"declare-{tool_call_id}",
        thread_id,
        "msg.create",
        {"role": role, "content": "", "tool_calls": calls},
        msg_id=msg_id or f"msg-{tool_call_id}",
    )


def _publish_source(
    db: ts.ThreadsDB,
    thread_id: str,
    tool_call_id: str,
    name: str,
    output,
    *,
    decision: str = "whole",
    preview: str | None = None,
    no_api: bool = False,
    approval: str = "granted",
) -> None:
    _declare(db, thread_id, tool_call_id, name)
    db.append_event(
        f"approval-{tool_call_id}",
        thread_id,
        "tool_call.approval",
        {"tool_call_id": tool_call_id, "decision": approval},
    )
    if approval == "granted":
        db.append_event(
            f"started-{tool_call_id}",
            thread_id,
            "tool_call.execution_started",
            {"tool_call_id": tool_call_id},
        )
        db.append_event(
            f"finished-{tool_call_id}",
            thread_id,
            "tool_call.finished",
            {"tool_call_id": tool_call_id, "reason": "success", "output": output},
        )
        db.append_event(
            f"output-{tool_call_id}",
            thread_id,
            "tool_call.output_approval",
            {
                "tool_call_id": tool_call_id,
                "decision": decision,
                "preview": preview if preview is not None else str(output),
            },
        )
    db.append_event(
        f"published-{tool_call_id}",
        thread_id,
        "msg.create",
        {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": preview if preview is not None else str(output),
            **({"no_api": True} if no_api else {}),
        },
        msg_id=f"published-msg-{tool_call_id}",
    )


def _declare_extractor(
    db: ts.ThreadsDB,
    thread_id: str,
    tool_call_id: str = "extract-current",
    *,
    extra_calls: list[dict] | None = None,
) -> str:
    _declare(
        db,
        thread_id,
        tool_call_id,
        "extract_tool_output",
        arguments={"start_line": 1, "end_line": 2},
        extra_calls=extra_calls,
    )
    db.append_event(
        f"approval-{tool_call_id}",
        thread_id,
        "tool_call.approval",
        {"tool_call_id": tool_call_id, "decision": "granted"},
    )
    return tool_call_id


def _artifact_id(receipt: str) -> str:
    match = re.search(r"provider artifact ([a-z0-9]{8})", receipt)
    assert match, receipt
    return match.group(1)


def _extract(
    db: ts.ThreadsDB,
    thread_id: str,
    current_tool_call_id: str,
    args: dict,
) -> tuple[str, dict, bytes]:
    receipt = ts.create_default_tools().execute(
        "extract_tool_output",
        args,
        db=db,
        thread_id=thread_id,
        tool_call_id=current_tool_call_id,
    )
    artifact_id = _artifact_id(receipt)
    metadata, data = resolve_provider_output_bytes(
        Path.cwd(), db, thread_id, artifact_id
    )
    return receipt, metadata, data


@pytest.mark.parametrize(
    ("text", "start", "end", "expected", "total"),
    [
        ("one\ntwo\nthree\n", 2, 4, "two\nthree\n", 3),
        ("one\ntwo\nthree", 1, 2, "one\n", 3),
        ("one", 1, 2, "one", 1),
        ("one\n\nthree\n", 2, 3, "\n", 3),
    ],
)
def test_exact_half_open_line_ranges_preserve_endings_and_trailing_newline(
    text, start, end, expected, total
) -> None:
    selected = ts.extract_text_line_range(text, start, end)
    assert selected.text == expected
    assert selected.total_lines == total
    assert (selected.start_line, selected.end_line) == (start, end)


@pytest.mark.parametrize(
    ("start", "end", "message"),
    [
        (0, 2, "start_line"),
        (1, 1, "exclusive"),
        (2, 1, "exclusive"),
        (4, 5, "out of range"),
        (1, 5, "maximum exclusive end"),
        (True, 2, "start_line"),
    ],
)
def test_exact_half_open_line_ranges_reject_invalid_or_out_of_range(
    start, end, message
) -> None:
    with pytest.raises(ValueError, match=message):
        ts.extract_text_line_range("a\nb\nc", start, end)


def test_default_previous_partial_source_extracts_full_canonical_unprefixed_text(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    thread_id = ts.create_root_thread(db, name="extract")
    canonical = "# Skill: demo\n\nalpha\nbeta\ngamma\ndelta\n"
    numbered_preview = "1: # Skill: demo\n2: \n3: alpha\n\n[Raw output stored as artifact.]"
    _publish_source(
        db,
        thread_id,
        "skill-source",
        "skill",
        canonical,
        decision="partial",
        preview=numbered_preview,
    )
    current = _declare_extractor(db, thread_id)

    receipt, metadata, data = _extract(
        db, thread_id, current, {"start_line": 3, "end_line": 6}
    )

    assert data == b"alpha\nbeta\ngamma\n"
    assert b"3: " not in data
    assert metadata["presentation"] == "file"
    assert metadata["mime_type"] == "text/plain; charset=utf-8"
    assert metadata["provenance"] == {
        "kind": "tool_output_extraction",
        "source_tool_name": "skill",
        "source_tool_call_id": "skill-source",
        "source_thread_id": thread_id,
        "owner_thread_id": thread_id,
        "start_line": 3,
        "end_line": 6,
        "line_range_semantics": "1-based half-open [start_line, end_line)",
    }
    assert metadata["derived"]["selected_line_count"] == 3
    assert metadata["size_bytes"] == len(data)
    assert metadata["sha256"] in receipt


def test_default_selection_skips_latest_denied_or_omitted_publication(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    thread_id = ts.create_root_thread(db, name="eligible-default")
    _publish_source(db, thread_id, "eligible", "bash", "keep\nme\n")
    _publish_source(db, thread_id, "denied-latest", "bash", "denied\n", approval="denied")
    _publish_source(
        db,
        thread_id,
        "omitted-latest",
        "python",
        "omitted\n",
        decision="omit",
        preview="Output omitted.",
    )
    current = _declare_extractor(db, thread_id)

    _receipt, metadata, data = _extract(
        db, thread_id, current, {"start_line": 1, "end_line": 3}
    )

    assert data == b"keep\nme\n"
    assert metadata["provenance"]["source_tool_call_id"] == "eligible"


def test_explicit_prior_selection_and_terminal_sanitization(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    thread_id = ts.create_root_thread(db, name="explicit")
    _publish_source(db, thread_id, "first", "bash", "safe\x1b[2Jtext\r\nnext\x07\n")
    _publish_source(db, thread_id, "second", "python", "other\nsource\n")
    current = _declare_extractor(db, thread_id)

    _receipt, metadata, data = _extract(
        db,
        thread_id,
        current,
        {"start_line": 1, "end_line": 3, "source_tool_call_id": "first"},
    )

    assert data == "safetext\nnext�\n".encode()
    assert b"\x1b" not in data
    assert metadata["provenance"]["source_tool_call_id"] == "first"


def test_reader_source_coordinates_are_literal_reader_canonical_output(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    thread_id = ts.create_root_thread(db, name="reader-source")
    reader_output = "artifact_id: abc12345\nchunk_number: 1\n\nraw-one\nraw-two\n"
    _publish_source(
        db,
        thread_id,
        "reader",
        "read_long_tool_output",
        reader_output,
        preview="artifact_id: abc12345\nchunk_number: 1\n\n91: raw-one\n92: raw-two\n",
    )
    current = _declare_extractor(db, thread_id)

    _receipt, _metadata, data = _extract(
        db, thread_id, current, {"start_line": 4, "end_line": 6}
    )

    assert data == b"raw-one\nraw-two\n"


def test_extraction_rejects_hidden_denied_pending_omitted_current_and_cross_thread(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    thread_id = ts.create_root_thread(db, name="visibility")
    other = ts.create_root_thread(db, name="other")

    _publish_source(db, thread_id, "hidden", "bash", "hidden\n", no_api=True)
    current = _declare_extractor(db, thread_id, "extract-hidden")
    result = ts.create_default_tools().execute(
        "extract_tool_output",
        {"start_line": 1, "end_line": 2, "source_tool_call_id": "hidden"},
        db=db,
        thread_id=thread_id,
        tool_call_id=current,
    )
    assert "prior visible published" in result

    _publish_source(db, thread_id, "denied", "bash", "denied\n", approval="denied")
    current = _declare_extractor(db, thread_id, "extract-denied")
    result = ts.create_default_tools().execute(
        "extract_tool_output",
        {"start_line": 1, "end_line": 2, "source_tool_call_id": "denied"},
        db=db,
        thread_id=thread_id,
        tool_call_id=current,
    )
    assert "not approved" in result

    _declare(db, thread_id, "pending", "bash")
    db.append_event(
        "approval-pending", thread_id, "tool_call.approval", {"tool_call_id": "pending", "decision": "granted"}
    )
    current = _declare_extractor(db, thread_id, "extract-pending")
    result = ts.create_default_tools().execute(
        "extract_tool_output",
        {"start_line": 1, "end_line": 2, "source_tool_call_id": "pending"},
        db=db,
        thread_id=thread_id,
        tool_call_id=current,
    )
    assert "prior visible published" in result

    _publish_source(db, thread_id, "omitted", "bash", "secret\n", decision="omit", preview="Output omitted.")
    current = _declare_extractor(db, thread_id, "extract-omitted")
    result = ts.create_default_tools().execute(
        "extract_tool_output",
        {"start_line": 1, "end_line": 2, "source_tool_call_id": "omitted"},
        db=db,
        thread_id=thread_id,
        tool_call_id=current,
    )
    assert "omitted" in result

    result = ts.create_default_tools().execute(
        "extract_tool_output",
        {"start_line": 1, "end_line": 2, "source_tool_call_id": current},
        db=db,
        thread_id=thread_id,
        tool_call_id=current,
    )
    assert "cannot select itself" in result

    _publish_source(db, other, "cross-thread", "bash", "other\n")
    current = _declare_extractor(db, thread_id, "extract-cross")
    result = ts.create_default_tools().execute(
        "extract_tool_output",
        {"start_line": 1, "end_line": 2, "source_tool_call_id": "cross-thread"},
        db=db,
        thread_id=thread_id,
        tool_call_id=current,
    )
    assert "current thread" in result


def test_default_selection_uses_latest_prior_publication_despite_extraction_sibling(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    thread_id = ts.create_root_thread(db, name="parallel")
    _publish_source(db, thread_id, "prior", "bash", "one\ntwo\n")
    sibling = {
        "id": "parallel-sibling",
        "type": "function",
        "function": {"name": "python", "arguments": "{}"},
    }
    current = _declare_extractor(db, thread_id, extra_calls=[sibling])

    _receipt, default_metadata, default_data = _extract(
        db,
        thread_id,
        current,
        {"start_line": 1, "end_line": 2},
    )
    assert default_data == b"one\n"
    assert default_metadata["provenance"]["source_tool_call_id"] == "prior"

    _receipt, metadata, data = _extract(
        db,
        thread_id,
        current,
        {"start_line": 1, "end_line": 2, "source_tool_call_id": "prior"},
    )
    assert data == b"one\n"
    assert metadata["provenance"]["source_tool_call_id"] == "prior"


def test_non_text_and_invalid_ranges_return_short_errors_without_artifacts(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    thread_id = ts.create_root_thread(db, name="errors")
    _publish_source(db, thread_id, "non-text", "binary", 123)
    current = _declare_extractor(db, thread_id)
    tools = ts.create_default_tools()

    result = tools.execute(
        "extract_tool_output",
        {"start_line": 1, "end_line": 2},
        db=db,
        thread_id=thread_id,
        tool_call_id=current,
    )
    assert "non-text/binary" in result

    # Publish a textual source after the failed direct call; direct execution did
    # not create lifecycle events for the extraction declaration.
    _publish_source(db, thread_id, "text", "bash", "a\nb\n")
    current = _declare_extractor(db, thread_id, "extract-range")
    for args, expected in [
        ({"start_line": 0, "end_line": 2}, "start_line"),
        ({"start_line": 1, "end_line": 1}, "exclusive"),
        ({"start_line": 3, "end_line": 4}, "out of range"),
        ({"start_line": 1, "end_line": 4}, "maximum exclusive end"),
    ]:
        result = tools.execute(
            "extract_tool_output",
            args,
            db=db,
            thread_id=thread_id,
            tool_call_id=current,
        )
        assert expected in result

    root = tmp_path / ".egg" / "egg_provider_output"
    records = [path for path in root.rglob("metadata.json")] if root.exists() else []
    assert records == []


def test_numbered_long_skill_preview_only_numbers_body_and_extraction_is_canonical(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EGG_OUTPUT_OPTIMIZER", "on")
    monkeypatch.setattr("eggthreads.runner.LONG_OUTPUT_LINE_THRESHOLD", 5)
    monkeypatch.setattr("eggthreads.runner.LONG_OUTPUT_CHUNK_LINES", 3)
    db = _make_db(tmp_path)
    thread_id = ts.create_root_thread(db, name="numbered-skill")
    _declare(
        db,
        thread_id,
        "skill-numbered",
        "skill",
        arguments={"name": "rlm", "line_numbers": True},
    )
    db.append_event(
        "approve-skill-numbered",
        thread_id,
        "tool_call.approval",
        {"tool_call_id": "skill-numbered", "decision": "granted"},
    )

    runner = ts.ThreadRunner(db, thread_id, llm=object())
    assert asyncio.run(runner.run_once()) is True
    state = ts.build_tool_call_states(db, thread_id)["skill-numbered"]
    canonical = state.finished_output
    assert canonical and canonical.startswith("# Skill: rlm\n\n")
    assert not canonical.startswith("1: ")

    approval = db.conn.execute(
        "SELECT payload_json FROM events WHERE thread_id=? AND type='tool_call.output_approval' ORDER BY event_seq DESC LIMIT 1",
        (thread_id,),
    ).fetchone()
    payload = json.loads(approval[0])
    assert payload["decision"] == "partial"
    preview_body, recovery = payload["preview"].rsplit("\n\n[", 1)
    assert preview_body.startswith("1: # Skill: rlm\n2: ")
    assert recovery.startswith("Preview only")
    assert not recovery.startswith("1: ")
    assert "optimizer" not in payload["channels"]
    artifact_dir = Path(payload["artifact_path"])
    assert (artifact_dir / "chunk-0001.txt").read_text().startswith("# Skill: rlm\n\n")
    if ts.build_tool_call_states(db, thread_id)["skill-numbered"].state != "TC6":
        assert asyncio.run(runner.run_once()) is True
    assert ts.build_tool_call_states(db, thread_id)["skill-numbered"].state == "TC6"

    _declare(
        db,
        thread_id,
        "extract-after-skill",
        "extract_tool_output",
        arguments={"start_line": 17, "end_line": 38},
    )
    db.append_event(
        "approve-extract-after-skill",
        thread_id,
        "tool_call.approval",
        {"tool_call_id": "extract-after-skill", "decision": "granted"},
    )
    assert asyncio.run(runner.run_once()) is True
    if ts.build_tool_call_states(db, thread_id)["extract-after-skill"].finished_output is None:
        assert asyncio.run(runner.run_once()) is True

    extract_state = ts.build_tool_call_states(db, thread_id)["extract-after-skill"]
    artifact_id = _artifact_id(extract_state.finished_output or "")
    _metadata, data = resolve_provider_output_bytes(tmp_path, db, thread_id, artifact_id)
    expected = ts.extract_text_line_range(canonical, 17, 38).text.encode()
    assert data == expected
    assert not re.search(rb"(?m)^\d+: ", data)


def test_reader_absolute_numbering_headers_and_split_mid_line_chunks(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("eggthreads.runner.LONG_OUTPUT_CHAR_THRESHOLD", 10)
    monkeypatch.setattr("eggthreads.runner.LONG_OUTPUT_CHUNK_CHARS", 5)
    monkeypatch.setattr("eggthreads.runner.LONG_OUTPUT_LINE_THRESHOLD", 100)
    db = _make_db(tmp_path)
    thread_id = ts.create_root_thread(db, name="reader-lines")

    preview, artifact_path = stash_tool_output_and_build_preview(
        db, thread_id, "producer", "abcdefgh\nsecond\n", max_chars=1
    )
    assert preview and artifact_path
    artifact_id = Path(artifact_path).name
    tools = ts.create_default_tools()

    first = tools.execute(
        "read_long_tool_output",
        {"artifact_id": artifact_id, "chunk_number": 1, "line_numbers": True},
        db=db,
        thread_id=thread_id,
    )
    second = tools.execute(
        "read_long_tool_output",
        {"artifact_id": artifact_id, "chunk_number": 2, "line_numbers": True},
        db=db,
        thread_id=thread_id,
    )
    third = tools.execute(
        "read_long_tool_output",
        {"artifact_id": artifact_id, "chunk_number": 3, "line_numbers": True},
        db=db,
        thread_id=thread_id,
    )

    for rendered in (first, second, third):
        header, _body = rendered.split("\n\n", 1)
        assert header.startswith(f"artifact_id: {artifact_id}")
        assert not re.search(r"(?m)^\d+: (artifact_id|owner_thread_id|chunk_number)", header)
    assert first.split("\n\n", 1)[1] == "1: abcde"
    assert second.split("\n\n", 1)[1] == "1: fgh\n2: s"
    assert third.split("\n\n", 1)[1] == "2: econd"
    assert (Path(artifact_path) / "chunk-0002.txt").read_text() == "fgh\ns"


def test_bypass_tools_skip_optimizer_and_never_create_long_artifacts_on_violation(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EGG_OUTPUT_OPTIMIZER", "on")
    optimizer_calls: list[str] = []

    def fail_optimizer(*args, **kwargs):
        optimizer_calls.append("called")
        raise AssertionError("optimizer must not run")

    monkeypatch.setattr(
        "eggthreads.output_optimizer.create_default_output_optimizer", fail_optimizer
    )

    for index, tool_name in enumerate(("read_long_tool_output", "extract_tool_output")):
        db = ts.ThreadsDB(tmp_path / f"threads-{index}.sqlite")
        db.init_schema()
        thread_id = ts.create_root_thread(db, name=tool_name)
        tool_call_id = f"call-{index}"
        _declare(db, thread_id, tool_call_id, tool_name)
        db.append_event(
            f"approve-{index}",
            thread_id,
            "tool_call.approval",
            {"tool_call_id": tool_call_id, "decision": "granted"},
        )
        registry = ToolRegistry()
        registry.register(
            tool_name,
            "adversarial",
            {"type": "object", "properties": {}},
            lambda _args: "x" * 150_000,
        )
        runner = ts.ThreadRunner(db, thread_id, llm=object(), tools=registry)
        assert asyncio.run(runner.run_once()) is True

        state = ts.build_tool_call_states(db, thread_id)[tool_call_id]
        assert state.output_decision == "whole"
        assert state.last_output_approval_payload["artifact_path"] == ""
        assert state.last_output_approval_payload["bounded_contract_violation"] is True
        assert state.last_output_approval_payload["preview"].startswith("Error:")
        assert "read_long_tool_output(" not in state.last_output_approval_payload["preview"]

    assert optimizer_calls == []
    output_root = tmp_path / ".egg" / "egg_outputs"
    assert not output_root.exists() or list(output_root.rglob("metadata.json")) == []


def test_skill_and_tool_help_remain_on_normal_long_output_artifact_route(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("EGG_OUTPUT_OPTIMIZER", raising=False)
    for index, tool_name in enumerate(("skill", "tool_help")):
        db = ts.ThreadsDB(tmp_path / f"ordinary-{index}.sqlite")
        db.init_schema()
        thread_id = ts.create_root_thread(db, name=tool_name)
        tool_call_id = f"ordinary-call-{index}"
        _declare(db, thread_id, tool_call_id, tool_name)
        db.append_event(
            f"ordinary-approve-{index}",
            thread_id,
            "tool_call.approval",
            {"tool_call_id": tool_call_id, "decision": "granted"},
        )
        registry = ToolRegistry()
        registry.register(
            tool_name,
            "ordinary long",
            {"type": "object", "properties": {}},
            lambda _args: "y" * 120_000,
        )
        runner = ts.ThreadRunner(db, thread_id, llm=object(), tools=registry)
        assert asyncio.run(runner.run_once()) is True
        state = ts.build_tool_call_states(db, thread_id)[tool_call_id]
        assert state.output_decision == "partial"
        assert state.last_output_approval_payload["artifact_path"]
        assert "read_long_tool_output(" in state.last_output_approval_payload["preview"]


def test_schema_help_wrappers_and_context_tool_call_id(tmp_path) -> None:
    registry = ts.create_default_tools()
    specs = {spec["function"]["name"]: spec["function"] for spec in registry.tools_spec()}
    assert specs["extract_tool_output"]["parameters"]["required"] == [
        "start_line",
        "end_line",
    ]
    assert specs["read_long_tool_output"]["parameters"]["properties"]["line_numbers"]["default"] is False
    assert specs["skill"]["parameters"]["properties"]["line_numbers"]["default"] is False

    help_text = registry.execute("tool_help", {"tool_name": "extract_tool_output"})
    assert "[17, 38)" in help_text
    assert "full sanitized canonical `tool_call.finished.output`" in help_text
    assert "read_long_tool_output" in help_text
    assert "source_tool_call_id" in help_text

    from eggthreads.session_runtime.tool_wrappers import generate_tool_wrappers_source

    ns = {"tool": lambda name, timeout_sec=None, **kwargs: (name, kwargs), "Any": object, "__name__": "test_wrappers"}
    exec(generate_tool_wrappers_source(list(specs.values())), ns, ns)
    assert "extract_tool_output" in ns["__all__"]
    assert ns["extract_tool_output"](17, 38) == (
        "extract_tool_output",
        {"start_line": 17, "end_line": 38},
    )

    captured = {}
    direct = ToolRegistry()
    direct.register(
        "capture",
        "capture",
        {"type": "object", "properties": {}},
        lambda _args, ctx: captured.update(tool_call_id=ctx.tool_call_id) or "ok",
        accepts_context=True,
    )
    assert direct.execute("capture", {}, tool_call_id="injected-call") == "ok"
    assert captured == {"tool_call_id": "injected-call"}

def test_explicit_python_filename_exports_exact_name_and_bytes(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    thread_id = ts.create_root_thread(db, name="python-export")
    ts.set_thread_working_directory(db, thread_id, "work")
    source_text = "def compact(messages):\n    return messages[-1:]\n"
    _publish_source(db, thread_id, "python-source", "skill", source_text)
    current = _declare_extractor(db, thread_id)

    _receipt, metadata, data = _extract(
        db,
        thread_id,
        current,
        {
            "start_line": 1,
            "end_line": 3,
            "filename": "compaction_skeleton.py",
        },
    )
    assert metadata["filename"] == "compaction_skeleton.py"
    assert data == source_text.encode("utf-8")

    artifact_id = metadata["artifact_id"]
    exported = ts.create_default_tools().execute(
        "save_provider_artifact_to_file",
        {"artifact_id": artifact_id},
        db=db,
        thread_id=thread_id,
    )
    payload = json.loads(exported)
    assert payload["path"] == "compaction_skeleton.py"
    output_path = tmp_path / "work" / "compaction_skeleton.py"
    assert output_path.name == "compaction_skeleton.py"
    assert output_path.read_bytes() == source_text.encode("utf-8")


def test_explicit_source_is_current_thread_only_across_ancestry(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    child = ts.create_child_thread(db, parent, name="child")
    _publish_source(db, parent, "parent-source", "bash", "parent\n")
    _publish_source(db, child, "child-source", "bash", "child\n")

    child_current = _declare_extractor(db, child, "child-extract")
    child_result = ts.create_default_tools().execute(
        "extract_tool_output",
        {"start_line": 1, "end_line": 2, "source_tool_call_id": "parent-source"},
        db=db,
        thread_id=child,
        tool_call_id=child_current,
    )
    assert "current thread" in child_result

    parent_current = _declare_extractor(db, parent, "parent-extract")
    parent_result = ts.create_default_tools().execute(
        "extract_tool_output",
        {"start_line": 1, "end_line": 2, "source_tool_call_id": "child-source"},
        db=db,
        thread_id=parent,
        tool_call_id=parent_current,
    )
    assert "current thread" in parent_result


def test_ra3_extraction_receives_runner_tool_call_id_and_creates_artifact(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    thread_id = ts.create_root_thread(db, name="ra3")
    _publish_source(db, thread_id, "ra3-source", "bash", "line-one\nline-two\n")
    tool_call_id = ts.enqueue_user_tool_call(
        db,
        thread_id,
        "extract_tool_output",
        {"start_line": 2, "end_line": 3, "source_tool_call_id": "ra3-source"},
        hidden=True,
        auto_approve=True,
    )

    runner = ts.ThreadRunner(db, thread_id, llm=object())
    assert asyncio.run(runner.run_once()) is True
    state = ts.build_tool_call_states(db, thread_id)[tool_call_id]
    assert state.finished_output and not state.finished_output.startswith("Error:")
    artifact_id = _artifact_id(state.finished_output)
    metadata, data = resolve_provider_output_bytes(tmp_path, db, thread_id, artifact_id)
    assert data == b"line-two\n"
    assert metadata["provenance"]["source_tool_call_id"] == "ra3-source"


def test_session_runtime_generated_module_exposes_extractor(tmp_path) -> None:
    import importlib.util
    import sys

    from eggthreads import session

    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    session._write_runtime_files(runtime_dir)
    eggtools_path = runtime_dir / "eggtools.py"
    old_module = sys.modules.pop("eggtools", None)
    try:
        spec = importlib.util.spec_from_file_location("eggtools", eggtools_path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules["eggtools"] = module
        spec.loader.exec_module(module)
        assert callable(module.extract_tool_output)
        with pytest.raises(TypeError, match="start_line"):
            module.extract_tool_output()
    finally:
        sys.modules.pop("eggtools", None)
        if old_module is not None:
            sys.modules["eggtools"] = old_module
