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
- [ ] Spawn one long-lived primary worker for focused implementation slices using the worker-manager workflow.

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

- [ ] Add effective recovery setting storage/resolution.
  - [ ] Event type suggestion: `thread.recovery`.
  - [ ] Field: `autoContinueOnError` boolean.
  - [ ] Default: enabled.
  - [ ] Inherit from nearest ancestor, like other thread settings where practical.
- [ ] Add `/toggleAutoContinueOnError` command.
  - [ ] With no argument: toggle current effective state.
  - [ ] Accept `on`/`off`/`true`/`false` if simple.
  - [ ] Log/status message reports new state.
  - [ ] Avoid adding broader settings commands.
- [ ] Tests:
  - [ ] Default is enabled.
  - [ ] Toggle off/on works.
  - [ ] Child inheritance works if implemented.

### Phase 3 — Isolated classification / retry-delay module

- [ ] Add `eggthreads/eggthreads/runner_recovery.py` (name can vary if better).
- [ ] Implement classification helpers.
  - [ ] Inputs can be exception text and/or persisted message payloads.
  - [ ] Return a structured decision: retriable? class/reason, delay, source summary, stop reason.
  - [ ] Recognize transient transport errors even when text contains `400`.
  - [ ] Recognize 5xx/429 status codes from text.
  - [ ] Exclude auth/context/permanent bad request/safety/quota/max-output classes.
- [ ] Implement retry-delay parser.
  - [ ] Parse `Retry-After`-like text when present.
  - [ ] Parse seconds/minutes forms: `retry after 30 seconds`, `try again in 2m`, `available in 60s`, `resets in 45 seconds`.
  - [ ] Clamp/decline according to max delay policy.
- [ ] Implement recovery-notice formatting helpers.
- [ ] Tests:
  - [ ] Classify the known `TransferEncodingError: 400 Not enough data...` example as retriable transport.
  - [ ] Classify timeout as retriable.
  - [ ] Classify 503/500 as retriable.
  - [ ] Classify generic 400/auth/context/quota/safety as non-retriable.
  - [ ] Parse retry delays.

### Phase 4 — Runner integration

- [ ] Wire Runner RA1 failure handling to recovery module.
  - [ ] After provider/runner error is persisted, ask recovery module whether to auto-continue.
  - [ ] Detect incomplete assistant messages and empty assistant system errors as sources.
  - [ ] Honor effective `autoContinueOnError` setting.
- [ ] Implement conservative attempt cap.
  - [ ] Max 1 automatic continue per source failure/failed turn by default.
  - [ ] Store enough metadata in recovery notices/control events to avoid repeated loops.
- [ ] Implement delay behavior.
  - [ ] Immediate/small async sleep inside runner is acceptable for initial version if it is fenced.
  - [ ] If provider-requested delay exceeds policy, append recovery notice and do not retry.
- [ ] Implement fence checks before applying delayed auto-continue.
  - [ ] Thread has no active lease except the just-finished runner invocation.
  - [ ] Source failure message still exists and is not skipped.
  - [ ] No newer normal user/tool trigger message appeared after the source failure.
  - [ ] No newer assistant/provider result appeared after the source failure.
  - [ ] Attempt cap not exceeded.
- [ ] On applying auto-continue:
  - [ ] Append scheduled/applied recovery notices as appropriate.
  - [ ] Call `continue_thread(...)` from the selected continue point.
  - [ ] Rebuild snapshot.
  - [ ] Let scheduler pick up the rerun normally.
- [ ] Tests:
  - [ ] RA1 503 error auto-continues and reruns once.
  - [ ] Transfer truncation text auto-continues and reruns once.
  - [ ] Timeout auto-continues once.
  - [ ] Generic 400 does not auto-continue.
  - [ ] Context-length path still uses existing recovery/does not auto-continue directly.
  - [ ] Fence cancels if a newer user message appears.
  - [ ] Attempt cap prevents loops.

### Phase 5 — Incomplete-response metadata improvements

- [ ] Preserve/propagate Responses API `response.incomplete` details when available.
- [ ] Ensure max-output-token truncation is distinguishable from transport early termination.
- [ ] Tests for incomplete classification and non-retry on max token truncation.

### Phase 6 — Optional UI polish

- [ ] If small, render `recovery_notice=True` system messages with a “Recovery” / “Continue Status” title instead of normal “System”.
- [ ] Do not block core recovery on this polish.
- [ ] Tests only if rendering changes are made.

## Status notes

- 2026-05-26 UTC: TODO created from user-approved design. Preflight: recent head `68834c9 long tool calls handling`; dirty state only `autocontinue.md` plus pre-existing untracked `count-lines.sh`. Existing untracked file `count-lines.sh` should not be touched by workers.


- 2026-05-26 UTC: Phase 1 implemented. Added reusable local recovery notice helpers in `eggthreads.api`; terminal Egg and Eggw manual `/continue` append recovery notices after successful continues; `continue_thread` preserves `preserve_on_continue` messages; diagnostics/status error checks ignore `recovery_notice` messages. Focused tests passed: `pytest -q eggthreads/tests/test_continue_thread.py eggthreads/tests/test_command_registry.py`; `PYTHONPATH=eggthreads pytest -q eggw/tests/test_api.py::TestMessageOperations`; `pytest -q eggthreads/tests/test_child_status.py eggthreads/tests/test_send_message_to_child.py eggthreads/tests/test_generic_user_tool_call_api.py::test_wait_for_threads_treats_llm_error_after_tool_message_as_completion eggthreads/tests/test_tool_state_runner_actionable.py`. Next: Phase 2 recovery setting storage and `/toggleAutoContinueOnError`.
