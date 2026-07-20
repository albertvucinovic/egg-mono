# `eggopt` Implementation TODO

This TODO operationalizes `plan.md`; the plan is authoritative. The manager owns architecture,
phase boundaries, and compliance review. A worker implements only the currently assigned leaf.

## Global invariants

- [x] Keep the public API domain-neutral; never import trading or ARC adapters.
- [x] Keep `Candidate` minimal: `Candidate(text: str)`; do not add URI/kind/codec/artifact storage.
- [x] Treat strategy/operator/solver/evaluator/aggregator/judge as Producer roles.
- [x] Keep one strategy transition: state + new observations -> next state + proposals or stop.
- [x] Preserve multiple good and bad observations and per-case evidence; do not collapse the core to
      one scalar-only observation.
- [x] Do not add first-class Check or Constraint primitives.
- [x] Expected invalid production becomes same-conversation repair feedback; infrastructure errors
      remain Eggflow failures; exhausted repair/context is terminal per item, not per study.
- [x] Support hard and soft composition without forcing either on consumers.
- [x] Use focused tests and one coherent commit per leaf; no broad review loops or speculative work.
- [x] Treat runtimes as injectable `Producer[StrategyRunInput, Task]` implementations that may be
  domain-owned; ship one Eggopt hierarchical runtime without a registry.
- [x] Make cached typed values plus explicit thread references authoritative; never scan raw names to
  recover hierarchy work.
- [x] Keep domain validation out of the generic runtime and do not add descendant-context REPL support.

## P5.3 — Persistent Solver/Execution composition

- [x] Replace overlapping leaf-thread and detached repair APIs with one domain-neutral
      `SolverExecution` Producer composition; prefer the term Execution because it covers parsing,
      compilation, tests, scoring, simulation, and judging.
- [x] Create/cache exactly one Solver thread and one Execution thread per item under an authoritative
      parent; reuse both IDs across all attempts and checks without scanning thread names.
- [x] Keep each solver attempt and execution/check independently cacheable by explicit identities,
      item/input digest, and attempt/check identity; changed output/check inputs must invalidate the
      relevant work without creating another pair.
- [x] Configure Solver and Execution through small pickle-safe specs. Execution owns an explicit
      working directory and sandbox policy and records actual Python/bash tool calls and outputs in
      its existing thread; do not create empty per-attempt validation threads.
- [x] Feed `NeedsRepair(RepairFeedback)` back into the same Solver thread, return `Accepted` typed
      values directly, return `ItemFailure` on exhaustion/recognized item terminal, and propagate
      unrelated infrastructure failures.
- [x] Make the common client program short and readable (one `SolverExecution(...)` construction and
      `.produce(request)`); remove or clearly replace obsolete `ThreadProducer`/repair surfaces rather
      than stacking adapters.
- [x] Focused tests must prove exact two-thread topology, real sandboxed execution history, attempt-
      scoped replay/invalidation, same-solver repair, fresh-executor resume, and capability isolation.
- [x] Update README/API/RUNNING docs with one minimal generic example; run Eggopt-focused checks and
      commit one coherent slice. Do not edit trading in this phase.

## P1 — Pure core

- [x] **P1.1 Package scaffold**
  - [x] Add standalone `eggopt` package metadata, README, and minimal monorepo install/test wiring.
  - [x] Require Python >=3.10 and keep the pure core dependency-free.
- [x] **P1.2 Core producer model**
  - [x] Add immutable `Candidate(text)`.
  - [x] Add generic `Producer[Input, Output]` and deterministic `FunctionProducer` composition.
  - [x] Add generic observation/evidence values capable of named metrics, feedback, and multiple
        good/bad examples without domain semantics.
  - [x] Add generic proposal and strategy input/decision/state contracts.
- [x] **P1.3 Deterministic GEPA**
  - [x] Select configurable parent set and selected good/bad evidence.
  - [x] Produce domain-neutral mutation requests for arbitrary candidate text.
  - [x] Enforce a simple deterministic budget/stop rule.
- [x] **P1.4 Deterministic PhysicsStrategy**
  - [x] Represent competing explanatory candidates and consistency evidence.
  - [x] Prefer a supplied model-consistent plan when credible.
  - [x] Otherwise request a cheap discriminating experiment or candidate revision.
  - [x] Enforce a simple deterministic experiment/round budget.
- [x] **P1.5 Focused validation and commit**
  - [x] Cover public invariants, multiple examples, stop behavior, and Producer substitutability.
  - [x] Build/import package and run only `eggopt` tests.
  - [x] Update this TODO and `plan.md` durable status; commit one P1 chunk.

## P2 — Durable deterministic transition

- [x] Add an Eggflow adapter without making Eggflow a pure-core dependency.
- [x] Give the strategy-transition task explicit identity covering strategy configuration and input.
- [x] Prove a second executor reuses `flow.db` without rerunning the strategy.
- [x] Keep live clients/schedulers out of task values.
- [x] Focused tests, status update, coherent commit.

## P3 — Eggthreads root and leaf producer

- [x] Cache creation of StudyRoot/StrategyRunRoot; committed cached thread ID is authoritative.
- [x] Implement one typed restricted leaf ThreadProducer under the root using mock/fake execution.
- [x] Preserve ancestry/capability boundaries and avoid manual “does this thread exist?” scans.
- [x] Reconstruct runtime resources after restart; do not pickle them.
- [x] Focused tests, status update, coherent commit.

## P4 — Same-conversation repair

- [x] Add repair composition around an inner Producer, not a Check hierarchy.
- [x] Run inspection as an ordinary task/Producer in a privileged ancestor.
- [x] Feed concrete sanitized invalid-output evidence back to the same inner conversation.
- [x] Key each attempt; distinguish infrastructure failure from repair feedback.
- [x] Convert exhausted repair/context to typed terminal item failure and prove a batch continues.
- [x] Focused tests, status update, coherent commit.

## P5 — Evaluation composition

- [x] Map candidate + cases through case Producers with cacheable per-case results.
- [x] Preserve all per-case results and aggregate through another Producer.
- [x] Permit deterministic, sandboxed, soft, and composite evaluator Producers.
- [x] Keep objectives/archives optional; Pareto is not mandatory.
- [x] Focused tests, status update, coherent commit.

## P5.1 — Library substrate closeout

- [x] Add one deterministic end-to-end substrate integration with flow.db replay.
- [x] Add concise stable API and optional-module documentation.
- [x] Run focused tests, package/import checks, and commit only library files.
- [x] Mark the library substrate ready for its first separately approved domain adapter.

## P5.2 — Generic hierarchical runtime (approved library slice)

- [x] Add dependency-free `StrategyRunInput` and typed run result values; keep the runtime structurally
  injectable as `Producer[StrategyRunInput, Task]` so domains can replace it.
- [x] Implement Eggopt's one hierarchical runtime with exact physical
  `StudyRoot/StrategyRunRoot/RunSetup/Step S000/Proposal P000` seed topology.
- [x] Run later steps and globally numbered proposals serially; give production, strategy transitions,
  cases, and aggregation physical threads, with no operation thread for bookkeeping or generic
  validation.
- [x] Support positive configurable `max_concurrent_cases`; retain request order before aggregation.
- [x] Return each deterministic operation's value with its authoritative cached thread ID; prove a
  fresh executor replays without producer calls or thread creation and without raw name scans.
- [x] Use fakes only, run focused tests/checks, update status, and commit one coherent library change.

## P5.2.1 — Contextual operation correction

- [x] Add dependency-free `OperationContext` / `OperationInput[T]`; invoke every configured role with
  its semantic value and authoritative operation thread ID, preserving the top-level runtime contract.
- [x] Use public Eggthreads APIs to write compact model-hidden operation start/outcome audit entries;
  include identities and digests, record infrastructure failure type, and write nothing on replay.
- [x] Retain candidate and ordered case `ItemFailure` outcomes, skip unavailable evaluation or
  aggregation, and continue sibling proposals/later steps from successful observations only.
- [x] Prove contextual restricted-child ancestry, exact skeleton/concurrency preservation, audit replay,
  infrastructure failure audit, and candidate/case failure isolation with fakes only.
- [x] Amend docs, run focused/full Eggopt tests and package checks, then commit one correction.

## P5.2.2 — Reusable contextual setup and GEPA selectors

- [x] Expose the existing minimal `OperationTask`; do not add a registry or parallel operation layer.
- [x] Add an optional configured setup Producer/identity/name beneath `RunSetup`; its effective
  `StrategyRunInput` drives seed/state/cases and its identity/config enters runtime cache identity.
- [x] Refactor pure GEPA decisions through one shared builder and add a contextual GEPA adapter with
  `ParentSelection` and per-parent `EvidenceSelection` child `OperationTask`s.
- [x] Cover setup-driven cases, exact selector ancestry/parity, selector/setup cache identity, and replay
  with zero Producer calls, threads, or audits.
- [x] Update concise docs/status, run full Eggopt checks, and commit only this generic slice.

## P6 — Trading adapter (domain repository)

- [ ] Follow the trading integration plan; no domain code in `eggopt`.
- [ ] Deterministic cached GEPA transition over development-only fixture evidence first.
- [ ] Any live mutation requires explicit approval; keep base prompt frozen and July unopened.

## P7 — ARC-AGI-3 adapter (domain folder)

- [ ] Follow the ARC plan; no ARC code in `eggopt`.
- [ ] Timeline + scripted compile/backtest repair before real environment effects.
- [ ] One replay-safe offline action only when local game files are available.

## P8 — Demand-driven extensions

- [ ] Add Best-of-N only when a domain needs exact hard fan-out/judging.
- [ ] Add Pareto/evolution/islands only when a concrete adapter exercises them.

## Status log

- 2026-07-20: P5.3 completed with one public `SolverExecution` composition, exactly one cached
  Solver/Execution sibling pair per item, attempt/check scoped caching, real sandboxed Python/bash
  history in Execution, cumulative same-Solver repair, typed terminal outcomes, and infrastructure
  error propagation. Focused tests cover exact topology, replay/invalidation, fresh executors, and
  capability isolation. The obsolete fake `ThreadProducer` and detached `eggflow_repair` APIs/tests
  were removed and concise README/API/RUNNING guidance added. No trading repository was edited.

- 2026-07-20: P5.2.2 completed with public `OperationTask`, optional effective-input setup beneath
  `RunSetup`, and `ContextualGEPAStrategy` selector children sharing pure GEPA decision logic. Setup
  and selector cache identity plus zero-work replay are covered; full Eggopt tests/package checks pass.
  Trading, LLMs, validation, and descendant tooling remain unstarted.

- 2026-07-20: P5.2.2 approved: public reusable `OperationTask`, optional contextual setup beneath
  `RunSetup`, and contextual GEPA selector operation children sharing pure decision logic. Existing
  callers/topology defaults remain valid; no trading or speculative runtime features are included.

- 2026-07-20: P5.2.1 completed with explicit operation context, public-API model-hidden
  digest/outcome audit messages, cached audit replay, and isolated typed candidate/case `ItemFailure`
  results. Tests prove domain child ancestry, failure audit, sibling/later-step continuation, ordered
  case retention, and preserved exact topology/concurrency. Full Eggopt tests and package checks pass;
  selector subthreads and trading remain unstarted.

- 2026-07-20: Manager review opened P5.2.1 to correct three runtime foundations before trading:
  explicit operation context for domain-owned children, model-hidden digest/outcome audit records, and
  isolated typed candidate/case `ItemFailure` outcomes. The exact skeleton, concurrency contract, and
  top-level runtime Producer contract stay unchanged; selector subthreads remain a later slice.

- 2026-07-19: P5.2 completed. Added pure `StrategyRunInput`/authoritative result values and one
  optional hierarchical runtime Producer returning an Eggflow Task. Focused fake tests prove exact
  seed and later hierarchy, serial proposals/steps, bounded ordered case execution, physical domain
  operation threads, cached thread/value replay, and no scans/model calls/validation stage. Focused
  Eggopt tests and package/import checks pass. P6 remains unstarted and requires separate domain work.

- 2026-07-19: P5.2 approved as the next library-only leaf. Decisions recorded before implementation:
  injectable/domain-owned runtime contract, one shipped hierarchy, exact `S000/P000` seed, serial
  steps/proposals, bounded ordered case concurrency, physical authoritative operations, no generic
  validation, authoritative cached refs+values, no name scans, no registry/model calls, and no general
  descendant-context REPL capability.

- 2026-07-19: Manager created this hierarchical TODO from the approved architecture after removing
  the premature implementation. Earliest incomplete leaf: P1.1–P1.2 as one minimal core slice.

- 2026-07-19: P1.1–P1.2 completed in a fresh implementation: standalone dependency-free package,
  immutable core values, synchronous runtime-checkable Producer composition, evidence preservation,
  and tagged one-transition strategy contracts. Focused core tests pass; package build/import checks
  pass. Review follow-up replaced unpicklable structured feedback mappings with an immutable,
  value-based, pickle-safe mapping and covered cache-value round trips. Next leaf: P1.3.

- 2026-07-19: P1.3 completed with deterministic `GEPAStrategy` Producer transitions, injected parent
  and evidence selector Producers, stable multi-parent proposal generation, aggregate feedback
  preservation, membership validation, and generation/no-input/no-selection stop rules. Focused
  `eggopt` tests pass. Next leaf: P1.4.

- 2026-07-19: P1.4 completed with deterministic `PhysicsStrategy` Producer transitions and injected
  hypothesis-selection, credible-plan, discriminating-experiment, and hypothesis-revision Producer
  roles. It supports empty-input model invention, identity-checks selected hypotheses, increments one
  round per Advance, and stops for exhausted budget or unavailable revisions. Focused tests pass.
  Next leaf: P1.5.

- 2026-07-19: P1.5 closeout audit found the existing focused tests already cover public invariants,
  multiple examples/feedback, stop behavior, both strategies as runtime Producers, and all three
  Physics branches. Focused tests, package build, isolated wheel import, and Python 3.10 compatibility
  checks pass. No tracked correction or empty commit was needed. P1 is complete; next phase: P2.

- 2026-07-19: P2 completed with optional generic `ProduceTask`/`EggflowProducer` composition. Cache
  identity uses an explicit caller-owned Producer identity plus a protocol-5 pickle digest of input;
  a second TaskStore/executor and new Producer reuse the committed decision without rerunning it.
  Pure imports remain Eggflow-free, live resources are documented out of cached values, and focused
  tests pass. Next phase: P3.

- 2026-07-19: P3 completed with optional pickle-safe Eggthreads specs/refs, cached creation of one
  StudyRoot/StrategyRunRoot hierarchy, and a typed fake `ThreadProducer` leaf that records an
  inspectable system/user/assistant transcript. Cached IDs/results are authoritative across fresh
  stores/executors without scans or repeated drive calls; DB resources are reconstructed per task and
  closed. Focused tests pass. Next phase: P4.

- 2026-07-19: P4 completed with dependency-free repair values and optional durable
  `RepairingProducer` composition. Expected invalid outputs accumulate concrete feedback on the same
  inner Producer instance; attempts and inspections are independently cached. Accepted normalized
  values return directly, exhaustion/context becomes typed per-item failure, batches continue, and
  nonterminal infrastructure errors remain failures. Focused tests pass. Next phase: P5.

- 2026-07-19: P5 completed with dependency-free ordered case/evaluation requests and optional
  durable `EvaluationProducer` map/aggregate composition. Generic ProduceTask now flattens Task
  results, enabling task-backed case and aggregate roles. Per-case evidence/order is enforced,
  aggregate metrics/feedback remain additive, and focused cache/replay/validation tests pass. This
  demonstrates hard and task-backed/soft Producer composition without forcing either. Next domain
  phase: P6, only with separate manager approval/instructions.

- 2026-07-19: P5.1 library closeout completed with one deterministic end-to-end test spanning
  evaluation, GEPA proposal/evidence selection, candidate materialization, cumulative repair,
  PhysicsStrategy experiment selection, and flow.db replay with zero new role calls. Concise API
  boundaries are documented. The library substrate is ready for a separately approved first domain
  adapter; no P6 work has begun.
