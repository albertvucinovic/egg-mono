# Answer-user preserving LLM turn TODO

Goal: add a model-visible tool `answer_user_while_preserving_llm_turn` that lets the LLM send a user-facing interim answer/status while continuing the same assistant/tool turn, plus a `/btw` command that asks the LLM to use that tool.

## Behavior requirements

- Tool name: `answer_user_while_preserving_llm_turn`.
- Intended use: when the LLM wants to answer or update the user, but still continue working and not end the current turn.
- The message must be displayed in the transcript similarly to a normal assistant answer, but with a distinct color/style.
- The message must remain visible at `medium` and `min` `/displayVerbosity` levels; it must not be compacted into hidden/min summaries.
- The tool should be available to the LLM without requiring `/btw`; the LLM may use it whenever appropriate.
- Add `/btw <message>` command that appends an instruction/user message asking the LLM to answer using the new tool.

## Design decisions

- Represent interim answers as normal `msg.create` events with role `assistant` plus metadata `answer_user_preserve_turn=True`.
  - This reuses existing snapshot, transcript, and event-history machinery.
  - The message is not a provider-final assistant boundary and must not stop the runner from continuing the current tool loop.
- The answer tool implementation appends that assistant message immediately and returns a short tool result.
- The interim assistant note is display-only, but the published tool result should remain a normal provider-visible tool result so the provider sees a valid assistant tool_call/tool response pair. Do not use `keep_user_turn=True` for this assistant-originated tool result.
- Runner/tool-state should treat this tool as auto-approved, same as `compact_thread`, because it is a user-facing communication primitive.
- Provider context sanitization must exclude interim assistant messages (`answer_user_preserve_turn=True`) so they are display/UI history, not protocol assistant turns sent back to the model.
- UI rendering should branch before normal assistant rendering and use a distinct style/color. Tentative title: `Assistant Note`; tentative color: bright magenta/magenta.

## Phase 1 — Core tool and runner semantics

- [x] Add a built-in tool plugin/registration for `answer_user_while_preserving_llm_turn`.
- [x] Tool schema should require a string message/content field.
- [x] Tool appends a `msg.create` assistant payload with:
  - `role='assistant'`
  - `content=<message>`
  - `answer_user_preserve_turn=True`
  - current `model_key` when known
  - optional tool_call_id/source metadata if useful locally
- [x] Make the tool auto-approved.
- [x] Ensure published tool result remains provider-visible as a normal tool response and does not use `keep_user_turn=True`; only the separate interim assistant note is excluded from provider context.
- [x] Ensure provider API messages exclude `answer_user_preserve_turn` messages.
- [x] Add focused eggthreads tests:
  - default tool registry includes the tool;
  - executing the tool appends the interim assistant message;
  - a model can call the tool and then continue to a final assistant message in the same overall workflow;
  - provider context sent after the tool result excludes the interim answer while retaining the assistant tool_call and matching tool result.

## Phase 2 — Transcript display semantics

- [x] Render `answer_user_preserve_turn` assistant messages as a distinct assistant-note style/color in static transcript.
- [x] Ensure `medium` and `min` verbosity still show the full message rather than compacting/hiding it.
- [x] Add focused egg UI tests for max/medium/min rendering behavior.

## Phase 3 — `/btw` command

- [x] Add `/btw <message>` command to the shared command registry.
- [x] The command appends a normal user message instructing the LLM to answer using `answer_user_while_preserving_llm_turn`, preserving the user-provided message.
- [x] The command should start/schedule the current thread like normal user input.
- [x] Add command tests for registration and appended message content.

## Phase 4 — Validation and polish

- [x] Run focused eggthreads and egg UI tests.
- [x] Run `git diff --check`.
- [x] Commit a small coherent implementation.
- [x] Report commit hash, tests, and any follow-up risks.

## Status notes

- 2026-05-16: Plan created. No implementation yet.

- 2026-05-16: Implemented Phases 1-4. Added the built-in `answer_user_while_preserving_llm_turn` tool and `/btw`, auto-approval and initial tool-result semantics, provider-context exclusion for interim notes, distinct assistant-note rendering at max/medium/min, and focused eggthreads/egg UI tests. Focused tests passed: `PYTHONPATH=eggthreads pytest -q eggthreads/tests/test_answer_user_preserve_turn.py`; `PYTHONPATH=egg:eggthreads pytest -q egg/tests/test_formatting.py::TestFormatMessagesText::test_answer_user_preserve_turn_note_visible_at_all_verbosities egg/tests/test_panels.py::TestConsolePrintMessage::test_answer_user_preserve_turn_note_visible_at_all_verbosities`.

- 2026-05-16: Corrected the provider protocol semantics after manual validation showed hidden answer-tool results left multiple assistant messages at the end for strict providers. The answer-tool result now remains a normal provider-visible tool response and no longer sets `keep_user_turn`; only the display-only `answer_user_preserve_turn` assistant note is excluded from provider context. Focused regression asserts the next provider call includes the assistant tool_call plus matching tool result and excludes only the interim note.
