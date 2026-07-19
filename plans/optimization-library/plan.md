# `eggopt` Optimization Library Plan

## Goal

Build a practically independent, domain-neutral optimization library in Egg Mono. It optimizes
arbitrary `Candidate(text)` values: prompts, code, policies, configurations, world models, or any
other text interpreted by a domain adapter.

`eggopt` supplies reusable optimization and execution composition. Trading and ARC-AGI-3 supply
all domain meaning, cases, effects, metrics, and feedback rendering. Neither domain may be imported
by the library.

## Agreed architecture

### Minimal public model

- `Candidate(text)` is the only mandatory candidate representation. A content hash may be derived
  internally for cache identity/deduplication; it is not part of the public model.
- `Producer[Input, Output]` hides how a typed result is made. A producer may be a deterministic
  function, one Eggthread, a sandboxed execution, or a hard/soft subtree.
- Strategy, mutation/operator, case solving, case evaluation, aggregation, and judging are semantic
  roles implemented with Producers, not separate execution primitives.
- A strategy has one transition: current state + new observations -> next state + proposals or stop.
- Per-case results remain available alongside aggregate evaluation. A domain may evaluate one case
  or an indivisible batch.
- Objectives/metrics and archives are optional domain/strategy inputs. Pareto is one reusable
  archive/selection implementation, never mandatory.

### Included strategies

- **GEPA:** select one or more parents and informative good/bad observations, then propose reflective
  candidate revisions. It must support multiple examples and arbitrary candidate text, not just
  prompts.
- **PhysicsStrategy:** maintain competing explanatory candidates, prefer a model-consistent plan,
  and otherwise request a cheap discriminating experiment. It is reusable for ARC world models and,
  later, empirical trading hypotheses.
- Both strategies start deterministic and may later be wrapped by soft LLM or hybrid producers.
  Consumers choose how deterministic or soft each role is.
- Evolution, multi-objective/Pareto evolution, hill climbing, random search, and island composition
  must fit the same strategy/producer contracts. Implement them only when a vertical slice needs
  them.

### Egg runtime boundary

- **Eggflow** is the durable control plane: meaningful work is cacheable/resumable in `flow.db`;
  irreversible effects cross an Eggflow task boundary; committed cache results, not scans for
  surplus threads, determine authoritative work.
- **Eggthreads** supplies inspectable ancestry, conversations, capability/tool scope, sandboxing,
  persistent soft reasoning, and correction conversations. A run may have a physical strategy root
  whether the strategy is deterministic, LLM-driven, or hybrid.
- Do not pickle live clients/schedulers. Reconstruct runtime resources after restart and drive the
  study subtree with the appropriate Eggthreads scheduler.
- Normal orchestration consumes explicit typed/cached results. Cached values and their explicit thread
  references are authoritative; orchestration never recovers work by scanning raw thread names. A soft
  strategy may optionally inspect descendant state/transcripts, but this slice does not add a general
  descendant-context REPL capability.
- A strategy runtime is injectable through the structural `Producer[StrategyRunInput, Task]` contract
  and may be owned by a domain. `eggopt` ships exactly one generic hierarchical runtime, not a runtime
  registry or a family of speculative runtimes.
- The generic runtime gives every authoritative domain operation a physical Eggthread. Pure
  bookkeeping does not gain operation threads. Domain validation is not a stage in the generic
  skeleton; a domain composes validation/repair into its Producers when required.
- Steps and proposals execute serially. Case operations may execute concurrently up to a positive,
  configurable `max_concurrent_cases`, while their results and aggregation remain in request order.
- Each configured domain role receives an explicit dependency-free operation context containing its
  authoritative physical thread ID, so domain-owned Producers may create restricted children under
  that operation without ambient context or name scans. Operation threads retain compact local-only
  audit entries (semantic name, Producer identity, input/output digests, and outcome/failure type);
  full inputs and outputs are not copied into transcripts.
- A typed `ItemFailure` from candidate production or case execution terminates only that
  proposal/evaluation. The ordered failure remains in the run result, unavailable downstream work is
  skipped, and sibling proposals plus later steps continue from successful observations. Infrastructure
  exceptions still fail the Eggflow task.

### Hard and soft composition

- Hard composition explicitly creates topology when correctness, permissions, budgets, caching,
  retry, reproducibility, or resumption depend on it.
- Soft composition lets a thread reason, delegate, or spawn subagents when exact topology is not a
  correctness requirement.
- Planned reusable hard combinators include `BestOfNProducer` and same-conversation repair. A hard
  producer may contain soft children.
- Privileged execution/supervision is an ancestor of restricted producer children. Sibling branches
  remain isolated and communicate through their common ancestor.
- Sandboxed adapters may use `outerContext/innerContext`: the execution ancestor sees hidden tests
  and the inner workspace; the restricted solver sees only the inner workspace and sanitized
  feedback.

### EvolveTropy-style repair without a `Check` primitive

There is no first-class `Check` or `Constraint` abstraction in v1. Domain inspection is an ordinary
Eggflow task or Producer supervised by a repair composition:

1. Start or continue the same inner producer conversation.
2. Inspect/execute its current output in the privileged ancestor.
3. Success returns the typed output.
4. Expected invalid output (parse, compile, tests, schema, etc.) becomes concrete sanitized feedback
   appended to that same producer conversation, then the next keyed attempt runs.
5. Infrastructure failure remains an Eggflow failure; it is not repair feedback.
6. Exhausted repair or context is terminal for that proposal/case. The item boundary converts it to
   a failed observation so the wider study continues.

A feedback/inspection role may itself be deterministic, sandboxed, LLM-based, or composite.

### Domain ownership

A domain adapter supplies:

- what `Candidate.text` means;
- cases and case-visible inputs;
- case producers/evaluators and aggregation;
- metrics/objectives and optional feasibility policy/archive;
- good/bad examples and safe feedback rendering;
- budgets and effect executors;
- hidden information and filesystem/tool boundaries.

The library supplies only reusable values, producer composition, strategies, durable runtime
adapters, and optional generic selection/archive utilities.

## Minimal target topology

```text
StudyRoot
└── StrategyRunRoot                         # passive, deterministic, soft, or hybrid
    ├── RunSetup
    ├── Step S000
    │   └── Proposal P000                   # seed
    │       ├── Production                  # candidate Producer operation
    │       └── Evaluation
    │           ├── Case K000               # case Producer operation
    │           ├── Case K001
    │           └── Aggregation             # aggregate Producer operation
    ├── Step S001                           # strategy transition, then proposals
    │   ├── StrategyTransition
    │   ├── Proposal P001
    │   └── Proposal P002
    └── Step S002                           # next serial transition
```

The shipped hierarchical runtime uses this exact physical skeleton. `S000/P000` is always the seed;
later steps and globally numbered proposals are serial. Production, strategy transition, each case,
and aggregation are authoritative domain operations and therefore have physical threads. Structural
nodes organize those operations; no additional thread is created for in-memory bookkeeping. Cached
result values paired with cached thread IDs are the only authoritative references.

## Agile implementation phases

- [x] **P1 — pure core.** Add the standalone `eggopt` package, `Candidate`, generic Producer,
  `FunctionProducer`, one strategy transition, and first deterministic GEPA/PhysicsStrategy behavior
  with focused tests.
- [x] **P2 — durable deterministic transition.** Add one Eggflow-backed strategy-transition
  producer/task with explicit cache identity and prove replay from `flow.db` without rerunning the
  strategy.
- [x] **P3 — Eggthreads run root and leaf producer.** Cache creation of a study/strategy root and
  implement one typed thread producer under it. Use fakes/mock mode; no live model call.
- [x] **P4 — same-conversation repair.** Implement a generic repair composition with keyed attempts,
  typed item failure, and focused tests proving feedback returns to the same producer while terminal
  item failure does not abort a containing batch.
- [x] **P5 — evaluation composition.** Map cases through case Producers, preserve all case results,
  aggregate them, and support deterministic or soft evaluator producers.
- [x] **P5.1 — library substrate closeout.** Demonstrate the deterministic substrate end to end,
  document the stable API/boundaries, and prove flow.db replay before any domain adapter.
- [x] **P5.2 — generic hierarchical runtime, first focused slice.** Add the one shipped
  `Producer[StrategyRunInput, Task]` runtime with the exact seed/step/proposal hierarchy, physical
  fake domain operations, bounded case concurrency, ordered aggregation, and replay-safe typed
  thread/value results. Do not add validation, a registry, model calls, or domain behavior.
- [x] **P5.2.1 — contextual operations, audit, and item isolation.** Pass explicit operation context to
  every configured role, record compact model-hidden operation audit entries, and retain candidate/case
  `ItemFailure` outcomes without aborting siblings or later successful observations. Preserve the exact
  skeleton, bounded ordered case behavior, and top-level runtime contract.
- [ ] **P6 — first domain vertical slice: trading.** Use existing development-only trading evidence
  for one cached deterministic GEPA transition, then one explicitly approved tiny live mutation.
  Keep the base prompt frozen and July unopened.
- [ ] **P7 — ARC-AGI-3 recorded trajectory.** Implement Timeline + compile/backtest repair against a
  scripted world-model candidate; then one resumable offline environment action when local game
  files are available.
- [ ] **P8 — search composition as demanded.** Add hard Best-of-N, Pareto/multi-objective selection,
  conventional evolution, and island wrappers only in response to concrete domain needs.

## Non-goals for the initial slices

- No artifact/URI/codec subsystem.
- No first-class Check or Constraint hierarchy.
- No mandatory Pareto archive, scalarization, LLM judge, or LLM strategy.
- No mandatory thread for cheap trusted calculations.
- No live provider calls, full ARC benchmark, island framework, or July trading access before the
  preceding deterministic slices prove caching, repair, and continuation.
- No high-assurance provenance subsystem or broad adversarial review loop.

## Acceptance rules

- Small, typed, domain-neutral public API; no trading or ARC imports.
- Focused tests cover each new behavior and one likely failure; do not repeatedly run unrelated full
  suites.
- Each slice updates this plan and commits one coherent change without unrelated refactoring.
- Provider/time budgets and expansion of scope require explicit approval.
- Generated plans for trading and ARC remain domain-side and map to this architecture rather than
  duplicating the library.

## Durable status

- 2026-07-20 — P5.2.1 completed. Every configured role now receives explicit
  `OperationInput(value, OperationContext)` with its authoritative operation thread; a focused fake
  creates a restricted child under that ID. Physical operations record compact `no_api` digest/outcome
  audit messages, including infrastructure exception type, while cached replay writes nothing. Candidate
  and ordered case `ItemFailure` results remain typed proposal outcomes, skip unavailable downstream
  work, and allow siblings/later steps to continue from successful observations only. Exact topology,
  serial steps/proposals, bounded ordered cases, and the top-level runtime contract remain unchanged.
  No selector subthreads or domain work were added.

- 2026-07-20 — Manager review opened focused correction P5.2.1 before any domain adapter: configured
  roles need explicit authoritative operation context for domain-owned restricted children; physical
  operations need compact local-only digest/outcome audit entries; and candidate/case `ItemFailure`
  values must remain typed item outcomes rather than aborting the study. No selector subthreads, live
  scheduling, domain behavior, ambient context, or descendant tooling are in this correction.

- 2026-07-19 — P5.2 completed with dependency-free run inputs/results and Eggopt's one optional
  `HierarchicalRuntime`. The runtime is itself a `Producer` returning an Eggflow `Task`, creates the
  exact `StudyRoot/StrategyRunRoot/RunSetup/Step S000/Proposal P000` seed hierarchy, then executes
  serial strategy steps/proposals. Production, transition, case, and aggregation results pair values
  with authoritative thread IDs; cases honor bounded concurrency while aggregation remains ordered.
  Fake-only tests prove exact ancestry, no validation/bookkeeping operation thread, no name scans, and
  fresh-executor replay with no producer calls or thread creation. No trading work began.

- 2026-07-19 — The user approved the missing generic runtime skeleton as P5.2 before any domain
  adapter. Runtime implementations are injectable `Producer[StrategyRunInput, Task]` values and may
  remain domain-owned; Eggopt supplies one hierarchical implementation. Its physical seed is
  `StudyRoot/StrategyRunRoot/RunSetup/Step S000/Proposal P000`, followed by serial steps/proposals.
  Authoritative domain operations have threads, case concurrency is bounded and ordered, and cached
  thread/value results—not name scans—are authoritative. Validation and general descendant-context
  REPL support are explicitly outside this slice.

- 2026-07-19 — The premature implementation from commit `9a1bca3` was removed at the user’s
  request. This manager-authored plan is now the sole implementation authority. Start fresh at P1;
  do not infer API decisions from the deleted code.

- 2026-07-19 — P1.1–P1.2 are complete in a fresh implementation. `eggopt` now has standalone
  dependency-free Python >=3.10 packaging; immutable `Candidate`, metric/feedback/case evidence and
  observation values; synchronous runtime-checkable `Producer`/`FunctionProducer` composition; and
  generic `StrategyInput`, `Proposal`, `Advance`/`Stop` transition contracts. No strategy or runtime
  implementation was added. Focused tests and package build/import checks pass. A review follow-up
  made recursively frozen feedback mappings pickle-safe and verified all transition/cache values
  round-trip through pickle; next leaf is P1.3 deterministic GEPA.

- 2026-07-19 — P1.3 is complete. Deterministic `GEPAStrategy` composes injected parent/evidence
  selector Producers, preserves selector ordering and candidate-level feedback, creates configurable
  proposals for arbitrary candidate text, validates selected values belong to supplied observations,
  and stops for budget or absent selection. No ranking policy, domain semantics, mutation execution,
  PhysicsStrategy, or runtime integration was added. Focused tests pass; next leaf is P1.4.

- 2026-07-19 — P1.4 is complete. Deterministic `PhysicsStrategy` composes four injected Producer
  roles to select consistent explanatory observations, prefer a credible plan, otherwise request a
  discriminating experiment, or initialize/revise hypotheses when none survive. It enforces identity
  membership and a simple round budget without domain scores, plan/experiment types, effects, or
  ARC/trading semantics. Focused tests pass; next leaf is P1.5.

- 2026-07-19 — P1 is complete across commits `bc0005e`, `64eb5f7`, `9b0e67d`, and `2cb0434`.
  Closeout confirmed the small domain-neutral public API, dependency-free package, immutable and
  pickle-safe evidence/transition values, Producer composition, and deterministic GEPA and
  PhysicsStrategy transitions. Existing focused tests cover multiple evidence/feedback, strategy
  branches/stops, likely invalid outputs, and runtime Producer substitutability. The package builds
  and imports from an isolated wheel under Python >=3.10-compatible syntax. No closeout code change
  or empty commit was needed. Next phase is P2; no runtime work has begun.

- 2026-07-19 — P2 is complete. Optional `eggopt.eggflow` provides one reusable generic
  `EggflowProducer`/`ProduceTask` adapter for any synchronous Producer role. Keys contain a schema,
  explicit caller-owned semantic/configuration identity, and SHA-256 of protocol-5 pickled input;
  they do not derive identity from functions or live objects. Focused tests prove a new Producer and
  second TaskStore/executor reuse the cached strategy decision from one `flow.db`, while pure
  `eggopt` remains dependency-free. Live clients/schedulers remain outside cached values. No P3
  Eggthreads/runtime reconstruction work has begun.

- 2026-07-19 — P3 is complete. Optional `eggopt.eggthreads` defines pickle-safe run refs, leaf specs,
  and typed inputs/outputs; Eggflow tasks create a real StudyRoot/StrategyRunRoot and configured fake
  Producer child without name/existence scans. A process-local drive records system/user/assistant
  transcript evidence, while committed `RunRoots`/`ThreadOutput` IDs are authoritative and replay
  from `flow.db` avoids new children or drive calls. Each task reconstructs and closes ThreadsDB;
  live schedulers/clients, provider calls, sandboxing, and repair remain out of scope. Next phase: P4.

- 2026-07-19 — P4 is complete. Dependency-free `eggopt.repair` supplies cumulative feedback,
  accepted/repair inspection outcomes, repair input, and typed per-item failure without a Check or
  Constraint primitive. Optional `eggopt.eggflow_repair` reuses one process-local inner Producer,
  gives every production/inspection attempt an explicit cache identity, returns normalized accepted
  values, converts exhausted/terminal context to `ItemFailure`, and re-raises nonterminal
  infrastructure failures. Focused replay/batch tests pass. No P5 evaluation work has begun.

- 2026-07-19 — P5 is complete. Dependency-free case/evaluation requests feed optional durable
  `EvaluationProducer` map/aggregate composition, with each ordered case independently cached and the
  complete `CaseEvidence` tuple enforced across aggregation. Generic `ProduceTask` now flattens one
  returned Eggflow Task, so deterministic or task-backed/sandboxed/soft roles share the Producer
  interface. Aggregate metrics/feedback may be derived without replacing candidate/case evidence;
  objectives, archives, and Pareto remain optional and absent. No P6 domain work has begun.

- 2026-07-19 — P5.1 library closeout is complete. One deterministic integration maps an arbitrary
  code/world-model candidate through two cached cases and aggregation, selects multiple ordered GEPA
  evidence examples, materializes and repairs a candidate with cumulative feedback, selects a
  PhysicsStrategy experiment, and replays evaluation/repair from flow.db with zero new role calls.
  `eggopt/API.md` records only the stable concepts, optional modules, and hard boundaries. The
  library substrate is ready for a separately approved first domain adapter; no trading/ARC code or
  model call was added.
