# ActorCritic compaction answer binding

## Goal

Bind every Eggopt ActorCritic answer to the user turn opened by its tagged
Eggopt prompt, so a later automatic-compaction request and checkpoint cannot
replace a completed domain answer.

## Scope

- May edit `eggopt/eggopt/actor_critic.py` and Eggopt tests.
- Eggflow and Eggthreads remain read-only.
- Preserve long-lived actor threads and automatic compaction.
- Preserve replay/restart behavior and correction rounds.
- Do not mutate the existing trading run database.

## Plan

- [x] Implement a DRY turn-bounded answer lookup: after the tagged prompt and
      strictly before the next effective user message.
- [x] Use it consistently for persisted-answer reuse, waiting detection, and
      final answer retrieval.
- [x] Add focused generic tests for a completed answer followed by a compaction
      request/checkpoint and for an open current turn.
- [x] Add a GEPA mutation/replay regression proving a strict mutation envelope
      is retained when a later compaction checkpoint exists.
- [x] Run focused and full Eggopt tests plus Ruff/diff checks.
- [x] Verify read-only against the existing run transcript.

## Status notes

- 2026-07-30: Root cause confirmed read-only. The valid mutation response at
  event 1631382 was followed by an auto-compaction user request at 1631385 and
  checkpoint at 1634509. Current `_latest_answer` chose the checkpoint because
  it searched the unbounded tail after the Eggopt prompt.
- 2026-07-30: Implemented turn-bounded lookup in generic ActorCritic and added
  generic closed/open-turn tests plus a mutation restart/replay regression.
  Tests passed: `pytest -q eggopt/tests` (65 passed). Production Ruff and diff
  checks pass; `test_gepa.py` retains pre-existing import-order warnings.
- 2026-07-30: Read-only verification against the real Mutation thread selects
  the 7,266-byte strict mutation envelope at event 1631382, not the later
  compaction checkpoint at event 1634509.
- 2026-07-30: Versioned the generic ActorCritic semantic key from v1 to v2 so
  existing completed caches that captured a later checkpoint are not reused.
  The replay regression now preserves a completed bad v1 cache while proving
  v2 recovers the original mutation without another model call.
