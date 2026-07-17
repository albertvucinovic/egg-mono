from __future__ import annotations

import json
from pathlib import Path

import pytest


FIXTURE_PATH = (
    Path(__file__).resolve().parents[2]
    / "eggthreads"
    / "tests"
    / "fixtures"
    / "phase6-operational-recovery-interleaved.json"
)


def _records() -> list[dict]:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    return fixture["records"]


def _egg_message(record: dict) -> dict:
    return {
        "event_seq": record["event_seq"],
        "msg_id": record["id"],
        "role": record["role"],
        "content": record["content"],
        **({"recovery_notice": True} if record.get("recovery_notice") else {}),
    }


class _TextTranscriptApp:
    def __init__(self, level: str):
        self._display_verbosity = level
        self.db = None


@pytest.mark.parametrize("level", ["min", "medium", "max"])
def test_interleaved_operational_fixture_keeps_labels_order_and_content(level: str) -> None:
    from egg.formatting import FormattingMixin

    class App(FormattingMixin, _TextTranscriptApp):
        pass

    records = _records()
    messages = [_egg_message(record) for record in records]
    output = App(level).format_messages_text("fixture", messages=messages)

    positions = [output.index(record["id"]) for record in records]
    assert positions == sorted(positions)
    for record in records:
        expected = record["expected_presentation"]
        assert f"[{expected['label']}" in output
        expected_content = (
            expected["min_content"]
            if level == "min"
            else expected["medium_max_content"]
        )
        assert expected_content in output
        if level == "min" and record.get("recovery_notice"):
            assert output.count(record["content"]) == (
                1 if expected_content == record["content"] else 0
            )


@pytest.mark.parametrize("level", ["min", "medium", "max"])
def test_panel_interleaved_system_records_keep_shared_presentation(egg_app, level: str) -> None:
    egg_app._display_verbosity = level
    records = _records()

    for record in records:
        if record["role"] != "system":
            continue
        items = egg_app._static_transcript_message_renderables(_egg_message(record))
        panels = [item.renderable for item in items if getattr(item.renderable, "title", None)]
        assert len(panels) == 1
        panel = panels[0]
        title = str(panel.title)
        body = str(getattr(panel.renderable, "plain", panel.renderable))
        expected = record["expected_presentation"]
        assert expected["label"] in title
        expected_content = (
            expected["min_content"]
            if level == "min"
            else expected["medium_max_content"]
        )
        assert body == expected_content
