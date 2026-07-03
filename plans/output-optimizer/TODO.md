# Egg native output optimizer TODO

## Purpose

Build an Egg-native output optimizer that captures most RTK-style token-savings benefits while preserving Egg's stronger event-sourced, sandbox-aware architecture.

The optimizer must be a **presentation/publication layer**, not a command-execution layer:

```text
command/tool runs normally
  -> raw output is captured and stored in tool_call.finished
  -> Egg optimizer derives a compact LLM/UI preview
  -> raw output remains inspectable/recoverable through existing artifacts/events
```

## Non-negotiable invariants

- Raw tool output remains the source of truth.
  - Do **not** replace or mutate `tool_call.finished.output` with optimized output.
  - Optimization only affects publication decisions/previews/messages.
- Commands execute exactly as requested.
  - No command rewriting in the native optimizer.
  - No proxying execution through another binary.
- Sandboxing semantics are unchanged.
  - The optimizer runs after capture, so it must work with unsandboxed, SRT/bwrap, Docker sandbox, and persistent REPL outputs.
  - Never bypass sandbox by re-running a command outside it.
- Provider-visible output and UI/audit output remain distinct.
  - Optimized output may be provider-visible.
  - Raw output must remain available for UI/audit/recovery.
- Secret handling stays at provider boundaries.
  - The optimizer may reduce output, but must not be the only secret defense.
  - Existing provider-boundary masking must still run.
- Never make output worse.
  - If optimization fails, expands output, or is low-confidence, fall back to existing default behavior.
- Every meaningful optimizer action should be inspectable.
  - Record optimizer name/filter, raw/optimized sizes, reason, and fallback status in output-publication metadata.

## Architecture sketch

### Proposed modules

```text
eggthreads/eggthreads/output_optimizer/
  __init__.py
  core.py              # request/decision dataclasses, protocols, registry, orchestration
  generic.py           # ANSI/control cleanup, progress/noise cleanup, dedupe, bounded fallback
  classify.py          # command/output-shape classification helpers
  filters/
    __init__.py
    cargo.py
    pytest.py
    git.py
    grep.py
    find.py
    logs.py
    python_traceback.py
```

### Core model

```python
@dataclass(frozen=True)
class OptimizeRequest:
    tool_name: str
    tool_args: Mapping[str, Any]
    output: str
    finished_reason: str
    thread_id: str
    tool_call_id: str
    origin: str
    user_tool_call: bool
    metadata: Mapping[str, Any]

@dataclass(frozen=True)
class OptimizeDecision:
    optimized: bool
    output: str
    optimizer_name: str
    filter_name: str | None
    raw_chars: int
    optimized_chars: int
    savings_pct: float
    reason: str
    confidence: float
    metadata: Mapping[str, Any]
```

### Integration point

Use the existing `OutputPolicy` seam:

```text
runner._emit_auto_output_approval(...)
  -> OutputPolicyRequest(...)
  -> OutputPoliciesPlugin registers default + optimizer policy
  -> optimizer decides preview when safe and beneficial
  -> tool_call.output_approval.preview contains optimized content
  -> tool_call.finished.output remains raw
```

The existing default policy should remain the fallback.

## Phase checklist

### Phase 0 — Planning and baseline

- [x] Create this hierarchical TODO at `plans/output-optimizer/TODO.md`.
- [x] Confirm current tracked tree is clean except known unrelated untracked files.
- [x] Identify focused tests around output policies and runner tool publication.

### Phase 1 — Pure optimizer core, no behavior change

Goal: add the native optimizer library and tests without wiring it into output publication yet.

- [x] Add `eggthreads/eggthreads/output_optimizer/` package.
- [x] Implement immutable request/decision dataclasses.
- [x] Implement `OutputFilter` protocol or equivalent small interface.
- [x] Implement registry/orchestrator with:
  - [x] ordered filters;
  - [x] min-size threshold;
  - [x] confidence threshold;
  - [x] never-worse guard;
  - [x] exception-to-fallback behavior.
- [x] Implement generic filters/helpers:
  - [x] ANSI/control cleanup helper;
  - [x] progress/noise line suppression for obvious progress bars/spinners;
  - [x] repeated-line dedupe with counts;
  - [x] bounded head/tail fallback with omission note;
  - [x] size/savings metadata calculation.
- [x] Add unit tests using pure strings and fake filters:
  - [x] optimizer unavailable/no filters -> unchanged decision;
  - [x] beneficial filter accepted;
  - [x] expanding filter rejected;
  - [x] throwing filter rejected;
  - [x] generic dedupe works;
  - [x] bounded fallback preserves head/tail and reports omission.
- [x] Update status notes in this TODO.
- [x] Commit as one focused commit.

### Phase 2 — OutputPolicy integration behind disabled/default-safe switch

Goal: wire optimizer into output publication without changing default behavior unless explicitly enabled.

- [x] Extend `OutputPolicyRequest` population in runner to include:
  - [x] `tool_name`;
  - [x] parsed tool arguments when available;
  - [x] user-tool vs assistant-tool origin;
  - [x] finished reason;
  - [x] output size metadata.
- [x] Add `NativeOptimizerOutputPolicy`.
- [x] Gate policy with an explicit config/env switch, initially disabled by default.
- [x] Preserve default policy behavior exactly when disabled.
- [x] When enabled and optimization succeeds:
  - [x] publish optimized preview;
  - [x] include raw/optimized char counts and savings in channels metadata;
  - [x] preserve raw output in `tool_call.finished`;
  - [x] preserve existing long-output artifact behavior or add raw artifact where needed.
- [x] Tests:
  - [x] disabled switch matches current default output exactly;
  - [x] enabled optimizer changes only preview/message content;
  - [x] raw `tool_call.finished.output` remains raw;
  - [x] hidden `$$` command remains `no_api`.
- [x] Update TODO status and commit.

### Phase 3 — First semantic filters

Goal: capture high ROI coding-agent outputs.

- [x] Add classifier helpers for bash script text and output shape.
- [x] Implement semantic filters:
  - [x] `pytest` failure summary;
  - [x] `cargo test` failure summary;
  - [x] `rg`/`grep` grouping by file;
  - [x] `find` path grouping;
  - [x] `git status` compact status;
  - [x] `git diff` compact diff preview;
  - [x] generic Python traceback focus.
- [x] Each filter must be conservative:
  - [x] high-confidence `matches` logic for pytest, cargo, grep/rg, find/fd, git status, git diff, and Python traceback;
  - [x] fallback unchanged on parse ambiguity for pytest, cargo, grep/rg, find/fd, git status, git diff, and Python traceback;
  - [x] tests for non-matching similar output for pytest, cargo, grep/rg, find/fd, git status, git diff, and Python traceback.
- [x] Tests with representative raw pytest, cargo, grep/rg, find/fd, git status, git diff, and Python traceback outputs.
- [x] Update TODO and commit in small slices, not one giant commit.

### Phase 4 — User/thread configuration and commands

Goal: make optimizer controllable and inspectable.

- [ ] Add event-sourced per-thread/inherited optimizer config.
- [ ] Add terminal shared commands:
  - [ ] `/outputOptimizerStatus`
  - [ ] `/outputOptimizerOn`
  - [ ] `/outputOptimizerOff`
  - [ ] `/outputOptimizerMode conservative|balanced|aggressive`
- [ ] Add EggW command adapters and settings display if appropriate.
- [ ] Tests for inheritance, toggling, and terminal/EggW parity.
- [ ] Update TODO and commit.

### Phase 5 — UI observability

Goal: make optimization visible without clutter.

- [ ] Terminal output should expose concise optimizer metadata when useful.
- [ ] EggW should show a small badge or metadata line, e.g. `Egg optimized · 95% saved · raw available`.
- [ ] Raw artifact/link affordance should be discoverable.
- [ ] Display verbosity modes should remain respected.
- [ ] Tests for event metadata/API shape and frontend rendering where feasible.
- [ ] Update TODO and commit.

### Phase 6 — Optional RTK adapter/reference backend

Goal: optionally use RTK as one backend/filter provider, not as the foundation.

- [ ] Add optional adapter that can run `rtk pipe` on captured output.
- [ ] Force privacy-safe env defaults:
  - [ ] `RTK_TELEMETRY_DISABLED=1`;
  - [ ] isolated/disabled tracking unless user explicitly opts in.
- [ ] Never depend on RTK availability for native optimizer behavior.
- [ ] Adapter failures/timeouts must fallback cleanly.
- [ ] Tests with fake RTK binary.
- [ ] Update TODO and commit.

## Initial implementation recommendation

Start with Phase 1 only. Do not wire the optimizer into runtime behavior until the pure library has focused tests and stable fallback semantics.

## Status notes

- 2026-07-02: Plan created. Latest repository state before implementation had only unrelated untracked `count-lines.sh` and latest commit `bb00ea8 none`.
- 2026-07-02: Phase 0 baseline: tracked tree clean; `plans/` is ignored, so this TODO must be force-added when committing. Focused tests to extend later include `eggthreads/tests/test_output_optimizer.py` for pure optimizer behavior and existing runner/output-policy tests around `tool_call.output_approval` publication.
- 2026-07-03: Phase 1 implemented as a pure `eggthreads.output_optimizer` package with immutable request/decision models, ordered fallback-safe orchestrator, generic cleanup/dedupe/bounded helpers, and focused unit tests. No runtime output-policy/runner integration was added.
- 2026-07-03: Phase 2 disabled-by-default integration implemented. `NativeOptimizerOutputPolicy` is registered after default output policy and gated by `EGG_OUTPUT_OPTIMIZER`/config truthy values; runner now supplies tool name/args, RA origin, user-tool flag, finished reason, and size/cap metadata. Enabled generic optimization rewrites publication previews/messages only, preserves `tool_call.finished.output`, and keeps long-output raw artifact recovery metadata.
- 2026-07-03: Phase 3 first slice implemented conservative classifier helpers plus a grep/rg semantic filter. The enabled native optimizer now tries grep/rg grouping before generic fallback; disabled behavior is unchanged. Remaining semantic filters are still pending.
- 2026-07-03: Phase 3 find/fd slice implemented conservative path-list grouping by directory. The enabled native optimizer now tries grep/rg, then find/fd, then generic fallback; disabled behavior remains unchanged. Pytest/cargo/git/traceback filters remain pending.
- 2026-07-03: Phase 3 git status slice implemented conservative `git status --short`/porcelain-v1 grouping by exact status code, including renamed/copied paths and caps. Enabled native optimizer order is grep/rg, find/fd, git status, then generic fallback. Pytest/cargo/git diff/traceback filters remain pending.
- 2026-07-03: Phase 3 Python traceback slice implemented a command-agnostic high-confidence traceback focus filter that preserves exception details and top/bottom frames while omitting middle frames with an explicit note. Enabled native optimizer order is grep/rg, find/fd, git status, Python traceback, then generic fallback. Pytest/cargo/git diff filters remain pending.
- 2026-07-03: Phase 3 git diff slice implemented a conservative unified `git diff` preview filter that preserves file names, hunk headers, and changed lines while omitting capped context/files/hunks with explicit notes. Enabled native optimizer order is grep/rg, find/fd, git status, git diff, Python traceback, then generic fallback. Pytest/cargo filters remain pending.
- 2026-07-03: Phase 3 pytest slice implemented a conservative pytest failure summary filter that preserves failed/error nodeids, assertion/error excerpts, and final summary while capping summaries/sections with explicit notes. Enabled native optimizer order is grep/rg, find/fd, git status, git diff, pytest, Python traceback, then generic fallback. Cargo remains pending.
- 2026-07-03: Phase 3 cargo slice implemented a conservative Cargo/Rust test failure summary filter that preserves failing test names, panic/source excerpts, final test result, and cargo error rerun lines while capping names/sections with explicit notes. All planned Phase 3 semantic filters are now implemented in small slices.
