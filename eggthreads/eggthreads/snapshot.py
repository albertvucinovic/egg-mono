from __future__ import annotations

from typing import Any, Dict, Iterable

from .projection import project_event_records


class SnapshotBuilder:
    """Compatibility builder backed by the canonical message projection core."""

    def build(self, events: Iterable[dict]) -> Dict[str, Any]:
        """Project loaded events and attach best-effort derived token metadata."""

        projection = project_event_records(events)
        snapshot = projection.to_snapshot_dict()
        try:
            from .token_count import snapshot_token_stats

            snapshot["token_stats"] = snapshot_token_stats(snapshot)
        except Exception:
            pass
        return snapshot
