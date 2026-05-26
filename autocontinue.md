# Auto-continue on provider/runner errors

## Goal

Implement conservative, enabled-by-default automatic recovery for transient provider/runner failures by continuing the thread from a safe point, while making both manual and automatic continuations leave persistent user-visible local status messages that are never sent to the provider.

## Hard constraints / decisions agreed with user

- Automatic error handling lives in the Runner path, not only in the UI command layer.
- Automatic handling is enabled by default; therefore the default policy must be conservative.
- Do **not** use Assistant Note / `answer_user_preserve_turn` as the status-message medium.
- Use persistent local system/recovery messages that are visible in the thread and excluded from provider context.
- Isolate the policy/classification concern in a separate module where practical.
- Do not add broad user-facing settings now.
- A small `/toggleAutoContinueOnError` command is acceptable.
- Keep changes focused; avoid unrelated refactors.

## Practical error classes to support

### Retriable by default

- Transport truncation / incomplete HTTP body:
  - `Response payload is not completed <TransferEncodingError: 400, message='Not enough data to satisfy transfer length header.'>`
  - `ClientPayloadError`, `ChunkedEncodingError`, `IncompleteRead`, `ServerDisconnectedError`, connection reset/disconnect text.
- Provider timeout / no response:
  - `TimeoutError`, `asyncio.TimeoutError`, read/connect timeout text.
- Transient provider/server errors:
  - HTTP `500`, `502`, `503`, `504`, `520`, `524`, `529`.
- Rate limit / overloaded:
  - HTTP `429`, `rate limit`, `overloaded`, `temporarily unavailable`, with retry delay respected when reasonable.
- Empty assistant response:
  - `LLM error: empty assistant message returned by provider`.
- Incomplete assistant response when reason looks transport/provider-related:
  - `incomplete=True` with `incomplete_reason` mentioning early stream/transport/provider disconnect.

### Not retriable by default

- Generic HTTP `400` unless text matches transient/transport/rate-limit patterns.
- Auth/permission: `401`, `403`, invalid key, expired OAuth, forbidden model.
- Context length / prompt too long; existing compaction recovery should handle this path.
- Permanent invalid request/schema/unsupported parameter/tool schema errors.
- Content filter/safety/policy blocks.
- Quota/billing exhaustion.
- Max-output-token truncation / `max_output_tokens` / finish reason `length`.

## Policy defaults

- Default enabled: yes.
- Max automatic continues per failed turn/source failure: 1.
- Fallback retry delay:
  - transport/timeout: 2 seconds.
  - 5xx: 5 seconds.
  - empty/incomplete: 2 seconds.
- Respect provider retry hints when available.
- Do not auto-schedule very long provider-requested waits by default:
  - desired maximum accepted provider delay: 300 seconds.
  - for `429`, only auto-wait if parsed/provider delay is within policy; otherwise write a local recovery notice and stop.
- Add no public command for attempts/delays yet; keep those as internal constants or `RunnerConfig` fields only if that is clearly simpler.

## Desired local recovery notice shape

Persistent user-visible message, excluded from provider API:

```python
append_message(
    db,
    thread_id,
    "system",
    content,
    extra={
        "no_api": True,
        "recovery_notice": True,
        "preserve_on_continue": True,
    },
)
```

Expectations:

- Visible in snapshots and UI as a local system/recovery notice.
- Never sent to provider because of `no_api=True`.
- Not treated as an LLM/provider error by diagnostics or auto-recovery.
- Not skipped by later `/continue` because of `preserve_on_continue=True`.
- Existing UI can initially render this as a normal system message; a special “Recovery” title is optional and should only be added if small.

## Hierarchical implementation plan

### Phase 0 — Coordination / baseline

- [x] Create this TODO/handoff file.
- [x] Run preflight (`git status --short`, `git log --oneline -8`) and record relevant baseline.
- [x] Spawn one long-lived primary worker for focused implementation slices using the worker-manager workflow.

### Phase 1 — Local recovery notices and manual `/continue` status

- [x] Add a small helper for appending recovery notices, preferably in a low-level module that both commands and runner code can call.
  - [x] Message role: `system`.
  - [x] Extra flags: `no_api=True`, `recovery_notice=True`, `preserve_on_continue=True`.
- [x] Update `/continue` command path to append a descriptive recovery notice after a successful continue.
  - [x] Include source: manual `/continue`.
  - [x] Include continue point short ID / full ID where helpful.
  - [x] Include skipped count and role/type summary.
  - [x] Include previous provider/runner error or incomplete reason if one was skipped.
  - [x] Preserve existing command result/log behavior.
- [x] Ensure `continue_thread(...)` does not skip messages with `preserve_on_continue=True`.
- [x] Ensure diagnostics/continue-point logic ignores recovery notices where they would otherwise look like system errors.
- [x] Tests:
  - [x] Manual `/continue` appends one persistent local recovery notice.
  - [x] Notice has `no_api`, `recovery_notice`, `preserve_on_continue`.
  - [x] Notice survives a later continue.
  - [x] Diagnosis does not treat recovery notice as an error.

### Phase 2 — Recovery configuration and toggle command

- [x] Add effective recovery setting storage/resolution.
  - [x] Event type suggestion: `thread.recovery`.
  - [x] Field: `autoContinueOnError` boolean.
  - [x] Default: enabled.
  - [x] Inherit from nearest ancestor, like other thread settings where practical.
- [x] Add `/toggleAutoContinueOnError` command.
  - [x] With no argument: toggle current effective state.
  - [x] Accept `on`/`off`/`true`/`false` if simple.
  - [x] Log/status message reports new state.
  - [x] Avoid adding broader settings commands.
- [x] Tests:
  - [x] Default is enabled.
  - [x] Toggle off/on works.
  - [x] Child inheritance works if implemented.

### Phase 3 — Isolated classification / retry-delay module

- [x] Add `eggthreads/eggthreads/runner_recovery.py` (name can vary if better).
- [x] Implement classification helpers.
  - [x] Inputs can be exception text and/or persisted message payloads.
  - [x] Return a structured decision: retriable? class/reason, delay, source summary, stop reason.
  - [x] Recognize transient transport errors even when text contains `400`.
  - [x] Recognize 5xx/429 status codes from text.
  - [x] Exclude auth/context/permanent bad request/safety/quota/max-output classes.
- [x] Implement retry-delay parser.
  - [x] Parse `Retry-After`-like text when present.
  - [x] Parse seconds/minutes forms: `retry after 30 seconds`, `try again in 2m`, `available in 60s`, `resets in 45 seconds`.
  - [x] Clamp/decline according to max delay policy.
- [x] Implement recovery-notice formatting helpers.
- [x] Tests:
  - [x] Classify the known `TransferEncodingError: 400 Not enough data...` example as retriable transport.
  - [x] Classify timeout as retriable.
  - [x] Classify 503/500 as retriable.
  - [x] Classify generic 400/auth/context/quota/safety as non-retriable.
  - [x] Parse retry delays.

### Phase 4 — Runner integration

- [x] Wire Runner RA1 failure handling to recovery module.
  - [x] After provider/runner error is persisted, ask recovery module whether to auto-continue.
  - [x] Detect incomplete assistant messages and empty assistant system errors as sources.
  - [x] Honor effective `autoContinueOnError` setting.
- [x] Implement conservative attempt cap.
  - [x] Max 1 automatic continue per source failure/failed turn by default.
  - [x] Store enough metadata in recovery notices/control events to avoid repeated loops.
- [x] Implement delay behavior.
  - [x] Immediate/small async sleep inside runner is acceptable for initial version if it is fenced.
  - [x] If provider-requested delay exceeds policy, append recovery notice and do not retry.
- [x] Implement fence checks before applying delayed auto-continue.
  - [x] Thread has no active lease except the just-finished runner invocation.
  - [x] Source failure message still exists and is not skipped.
  - [x] No newer normal user/tool trigger message appeared after the source failure.
  - [x] No newer assistant/provider result appeared after the source failure.
  - [x] Attempt cap not exceeded.
  - [x] Manual `/continue` racing with delayed auto-continue cancels the retry.
- [x] On applying auto-continue:
  - [x] Append scheduled/applied recovery notices as appropriate.
  - [x] Call `continue_thread(...)` from the selected continue point.
  - [x] Rebuild snapshot.
  - [x] Let scheduler pick up the rerun normally.
- [x] Tests:
  - [x] RA1 503 error auto-continues and reruns once.
  - [x] Transfer truncation text auto-continues and reruns once.
  - [x] Timeout auto-continues once.
  - [x] Generic 400 does not auto-continue.
  - [x] Context-length path still uses existing recovery/does not auto-continue directly.
  - [x] Fence cancels if a newer user message appears.
  - [x] Attempt cap prevents loops.
  - [x] Toggle off disables auto-continue.
  - [x] Manual `/continue` race cancels pending auto-continue.
  - [x] Newer continue interrupt cancels pending auto-continue even if the source remains unskipped.

### Phase 5 — Incomplete-response metadata improvements

- [x] Preserve/propagate Responses API `response.incomplete` details when available.
- [x] Ensure max-output-token truncation is distinguishable from transport early termination.
- [x] Tests for incomplete classification and non-retry on max token truncation.

### Phase 6 — Optional UI polish

- [ ] If small, render `recovery_notice=True` system messages with a “Recovery” / “Continue Status” title instead of normal “System”.
- [ ] Do not block core recovery on this polish.
- [ ] Tests only if rendering changes are made.

## Status notes

- 2026-05-26 UTC: TODO created from user-approved design. Preflight: recent head `68834c9 long tool calls handling`; dirty state only `autocontinue.md` plus pre-existing untracked `count-lines.sh`. Existing untracked file `count-lines.sh` should not be touched by workers.


- 2026-05-26 UTC: Phase 1 implemented. Added reusable local recovery notice helpers in `eggthreads.api`; terminal Egg and Eggw manual `/continue` append recovery notices after successful continues; `continue_thread` preserves `preserve_on_continue` messages; diagnostics/status error checks ignore `recovery_notice` messages. Focused tests passed: `pytest -q eggthreads/tests/test_continue_thread.py eggthreads/tests/test_command_registry.py`; `PYTHONPATH=eggthreads pytest -q eggw/tests/test_api.py::TestMessageOperations`; `pytest -q eggthreads/tests/test_child_status.py eggthreads/tests/test_send_message_to_child.py eggthreads/tests/test_generic_user_tool_call_api.py::test_wait_for_threads_treats_llm_error_after_tool_message_as_completion eggthreads/tests/test_tool_state_runner_actionable.py`. Next: Phase 2 recovery setting storage and `/toggleAutoContinueOnError`.

- 2026-05-26 UTC: Phase 2 implemented. Added `thread.recovery` settings with `autoContinueOnError` defaulting enabled and nearest-ancestor inheritance; added terminal Egg and Eggw `/toggleAutoContinueOnError [on|off|true|false|1|0]`; Eggw settings endpoint now reports `autoContinueOnError`. Focused tests passed: `pytest -q eggthreads/tests/test_continue_thread.py eggthreads/tests/test_command_registry.py`; `PYTHONPATH=eggthreads pytest -q eggw/tests/test_api.py::TestAutoApproval eggw/tests/test_api.py::TestAutoContinueOnError eggw/tests/test_api.py::TestMessageOperations::test_web_continue_appends_recovery_notice`. Next: Phase 3 classification and retry-delay helpers.

- 2026-05-26 UTC: Phase 3 implemented. Added pure `runner_recovery.py` classification, retry-delay parsing, and recovery-notice formatting helpers with focused coverage for transient transport/timeout/5xx/429/empty/incomplete cases and non-retry exclusions. Tests passed: `pytest -q eggthreads/tests/test_runner_recovery.py` (14 passed). Next: Phase 4 Runner integration.

- 2026-05-26 UTC: Phase 4 implemented. Runner RA1 failures now classify persisted system errors/incomplete assistant messages, honor `autoContinueOnError`, append local scheduled/applied/stopped notices, enforce one automatic continue per triggering RA1 message, and fence delayed retries against active leases, skipped source failures/manual continues, newer user/tool triggers, newer provider results, and attempt caps. Context-length recovery remains on the compaction path. Tests passed: `pytest -q eggthreads/tests/test_runner_auto_continue.py eggthreads/tests/test_runner_recovery.py eggthreads/tests/test_scheduler_slots.py::TestContextLimit::test_global_context_limit_blocks_ra1_when_exceeded eggthreads/tests/test_scheduler_slots.py::test_runner_persists_partial_tool_call_on_provider_transport_error` (27 passed); `pytest -q eggthreads/tests/test_compaction.py::test_runner_recovers_context_length_provider_error_by_queueing_summary_next eggthreads/tests/test_scheduler_slots.py::TestContextLimit` (8 passed). Next: Phase 5 incomplete-response metadata improvements.

- 2026-05-26 UTC: Phase 5 implemented. Responses API sync/async adapters now preserve `response.incomplete` as assistant `incomplete=True` metadata with `incomplete_reason` and `incomplete_details` when provided; runner persistence keeps incomplete-only assistant messages instead of replacing them with empty-response errors; runner recovery classification also inspects structured incomplete details so `max_output_tokens` truncation stops instead of retrying while transport/provider early termination remains retriable. Focused tests passed: `pytest -q eggllm/tests/test_openai_responses.py::TestResponsesIncompleteMetadata eggthreads/tests/test_runner_recovery.py::test_incomplete_payload_with_max_output_details_is_not_retriable eggthreads/tests/test_runner_auto_continue.py::test_max_output_incomplete_metadata_does_not_auto_continue`; `pytest -q eggllm/tests/test_openai_responses.py eggthreads/tests/test_runner_recovery.py eggthreads/tests/test_runner_auto_continue.py` (64 passed). Next: Phase 6 optional UI polish.
