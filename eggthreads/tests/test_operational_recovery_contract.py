from __future__ import annotations

import json
from pathlib import Path


FIXTURE_PATH = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "phase6-operational-recovery-interleaved.json"
)
PUBLIC_MESSAGE_FIELDS = {
    "event_seq",
    "id",
    "role",
    "content",
    "recovery_notice",
}


def _records() -> list[dict]:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    return fixture["records"]


def _public_message(record: dict) -> dict:
    return {key: record[key] for key in PUBLIC_MESSAGE_FIELDS if key in record}


def test_literal_operational_recovery_fixture_preserves_canonical_order() -> None:
    records = _records()

    assert [record["event_seq"] for record in records] == list(range(10, 90, 10))
    assert [record["id"] for record in records] == [
        "phase6-user-00000010",
        "phase6-error-00000020",
        "phase6-retry-scheduled-00000030",
        "phase6-system-00000040",
        "phase6-retry-applied-00000050",
        "phase6-retry-stopped-00000060",
        "phase6-manual-continue-00000070",
        "phase6-assistant-00000080",
    ]


def test_current_cross_client_intersection_cannot_name_recovery_decisions() -> None:
    records = {record["id"]: record for record in _records()}
    scheduled = records["phase6-retry-scheduled-00000030"]
    applied = records["phase6-retry-applied-00000050"]
    stopped = records["phase6-retry-stopped-00000060"]
    manual = records["phase6-manual-continue-00000070"]
    error = records["phase6-error-00000020"]

    # The durable payload has structured retry decisions, but the existing
    # cross-client /messages intersection exposes only a generic notice marker.
    assert scheduled["canonical_payload"]["action"] == "scheduled"
    assert applied["canonical_payload"]["action"] == "applied"
    assert stopped["canonical_payload"]["action"] == "stopped"
    assert {
        key: value
        for key, value in _public_message(scheduled).items()
        if key not in {"event_seq", "id", "content"}
    } == {"role": "system", "recovery_notice": True}
    assert {
        key: value
        for key, value in _public_message(applied).items()
        if key not in {"event_seq", "id", "content"}
    } == {"role": "system", "recovery_notice": True}
    assert {
        key: value
        for key, value in _public_message(stopped).items()
        if key not in {"event_seq", "id", "content"}
    } == {"role": "system", "recovery_notice": True}

    # Manual continuation has no distinct structured marker, and runner_error is
    # not in the current EggW /messages model. Presentation code must not recover
    # either semantic by parsing mutable human-readable content.
    assert manual["canonical_payload"] == {}
    assert manual["recovery_notice"] is True
    assert error["canonical_payload"]["runner_error"] is True
    assert "runner_error" not in _public_message(error)
