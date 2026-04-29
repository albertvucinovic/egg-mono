# explicit-rlm implementation todo

This is a working, hierarchical checklist for implementing `explicit-rlm.md`.
Keep this file updated as phases land.

Legend:
- [x] done
- [~] in progress / partial
- [ ] not started

## Phase 0: Plan hygiene

- [x] Add runtime invariants to `explicit-rlm.md`.
- [x] Create this implementation todo.

## Phase 1: Capability model

- [x] Extend `ToolsConfig` with `allowed_tools: Optional[Set[str]]`.
- [x] Parse `tools.config` payload `allow_only`.
- [x] Add `ToolsConfig.is_tool_allowed(name)`.
- [ ] Add public APIs:
  - [x] `set_thread_tool_allowlist(db, thread_id, names)`
  - [x] `clear_thread_tool_allowlist(db, thread_id)`
- [x] Update RA1 tool spec filtering to use `is_tool_allowed`.
- [x] Update RA2/RA3 execution denial to use `is_tool_allowed`.
- [x] Export new APIs from `eggthreads.__init__`.
- [x] Tests for allowlist parsing/exposure/execution denial.

## Phase 2: Generic user tool-call enqueue/wait helpers

- [x] Add `ToolCallResult` dataclass.
- [x] Add `enqueue_user_tool_call(...)`.
- [x] Refactor `execute_bash_command(...)` to use generic helper.
- [x] Add `wait_for_tool_call_result(...)`.
- [x] Add async `wait_for_tool_call_result_async(...)`.
- [x] Add structured `ThreadWaitResult` / `wait_for_threads(...)`.
- [x] Refactor `wait` tool to use structured helper while preserving text output.
- [x] Export new helpers.
- [x] Tests for generic enqueue/wait.

## Phase 3: Scheduler resource split

- [x] Add `RunnerConfig.max_concurrent_llm_threads`.
- [x] Add optional `RunnerConfig.max_concurrent_tool_threads`.
- [x] Preserve `max_concurrent_threads` compatibility.
- [x] Add helper to classify RA as `llm` or `tool`.
- [x] Update `SubtreeScheduler.run_forever` so only RA1 consumes LLM slots.
- [x] Ensure tool-running threads are still considered running/leased.
- [~] Tests for scheduler resource classification (direct occupied-slot integration still pending).

## Phase 4: Runtime child threads

- [ ] Add `eggthreads/session.py` skeleton.
- [ ] Add runtime config event helpers.
- [ ] Add `get_or_create_runtime_thread(...)`.
- [ ] Runtime thread messages/config defaults (`no_api`, no LLM tools).
- [ ] Tests for runtime child creation/reuse/tree placement.

## Phase 5: Session config and lifecycle

- [ ] Add `SessionConfig` resolver for `session.config` events.
- [ ] Add fake/in-memory session provider for tests.
- [ ] Add Docker session provider skeleton.
- [ ] Add lifecycle events.
- [ ] Tests for config resolution and lifecycle events.

## Phase 6: Python REPL MVP

- [ ] Add `python_repl` tool.
- [ ] Add eval token registry / bridge skeleton.
- [ ] Add fake Python REPL provider for tests.
- [ ] Programmatic `eggtools` calls enqueue RA3 on runtime thread.
- [ ] Tests for Python state persistence and tool call enqueue/wait.

## Phase 7+: Later phases

- [ ] Bash REPL.
- [ ] Docker `egg-sessiond`.
- [ ] `eggtools.py` and `eggtool` in container.
- [ ] Spawn capability attenuation and session sharing.
- [ ] TUI/Web session commands.
