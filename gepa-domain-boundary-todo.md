# GEPA domain boundary refactor

## Goal

Make Eggopt GEPA an optimizer over opaque, durable candidate values. GEPA owns
selection, search state, evaluation scheduling, budgets, and Pareto admission.
The domain owns candidate representation and the Mutator task graph—including
Actors, prompts, extraction (response or filesystem), Critic validation, and the
accepted candidate returned to GEPA.

## Invariants

- GEPA does not define or parse a `Mutation` type or wire format.
- GEPA never applies component updates; a domain Mutator returns the complete
  next candidate.
- A successful deterministic Critic may return an extracted value. ActorCritic
  passes that value through without knowing its type.
- Candidate values are opaque to search and evaluators but must have a stable,
  finite JSON representation for Eggflow cache identity and restart durability.
- Domain code does not query Eggthreads SQL/storage directly.
- Existing ActorCritic recovery, correction loops, and workspace isolation stay
  reusable.

## Plan

- [x] Generalize `ActorCritic` accepted decisions with an optional opaque value.
- [x] Replace GEPA's built-in Mutation machinery with a domain Mutator
      task boundary that returns a complete candidate.
- [x] Generalize search/evaluation identity and results from string mappings to
      opaque finite JSON candidate values.
- [x] Move simpletrade request/prompt/envelope extraction and source validation
      into its domain Mutator/Critic implementation.
- [x] Add regressions for response extraction and real workspace-file
      extraction without GEPA knowing either transport.
- [x] Run focused/full Eggopt and simpletrade tests, Ruff, update docs, and commit
      coherent changes with tracked working trees clean.

## Status notes

- 2026-07-30: User clarified the boundary after commits `90fb063` and `185a27c`:
  allowing a domain Critic is insufficient while GEPA still owns `Mutation`,
  strict JSON parsing, component updates, and Actor prompts. Refactor started at
  the stronger opaque-candidate/complete-domain-Mutator boundary.
- 2026-07-30: Core boundary implemented. `MutatorInput` is the sole domain
  input; a Mutator returns a complete candidate. GEPA candidates are finite JSON
  values, and ActorCritic can pass a Critic-extracted accepted value. Eggopt
  tests pass (63). Next: migrate the three client domains and validate their
  minimal adapters before committing the implementation.
- 2026-07-30: Migrated all three concrete client shapes. Simpletrade owns its
  source-response parser and compile/smoke Critic; src-agile owns its complete
  system-prompt extractor/validator; ARC-AGI-3's existing file-backed Modeler
  Critic now returns the accepted `world_model.py` snapshot through
  `ActorCriticResult.value`. Tests: Eggopt 63, simpletrade 19, trading GEPA 64,
  ARC Physics 15; Ruff passes all touched implementation/tests.
- 2026-07-30: Removed the transitional three-positional-argument generator
  compatibility branch. Every Mutator now has the single haiku signature
  `mutator(context)`.
- 2026-07-30: Added a literal file-backed ActorCritic regression: the Actor's
  chat answer is only a completion signal, while the Task Critic reads
  `candidate.py` from the shared workspace and returns its extracted source as
  the accepted value.
- 2026-07-30: Preserved `GEPA → Mutation Review → Mutation → Reflection` as an
  authorization topology only. The generic Mutation thread is the execution
  anchor for domain Actor/Critic tasks, while the opaque Mutator executes from
  GEPA's study scope and carries no mutation schema or parser. This preserves
  existing run topology and domain-chosen Actor/Critic child names.
- 2026-07-30: Added the one generic operation-level knob
  `mutator_context_limit`; domain ActorCritic agents can still override it. This
  preserves the existing clients' explicit mutation budgets without GEPA
  inspecting a Mutator's implementation.
- 2026-07-30: Added typed `Critique.accept(value, feedback)` and
  `Critique.revise(feedback)` helpers, keeping domain Critics concise while
  preserving plain decision mappings for compatibility.
- 2026-07-30: Hardened callable Mutator identity: immutable configuration state
  participates automatically when JSON-safe; stateful/runtime-mutating
  callables must expose `get_cache_key()`. Both trading client factories now do.
- 2026-07-30: Final verification: Eggopt 63, simpletrade 19, trading GEPA 64,
  ARC Physics 15. Ruff and format checks pass in every touched client. No
  mutation schema, component name, response format, or artifact transport
  remains in Eggopt GEPA.
