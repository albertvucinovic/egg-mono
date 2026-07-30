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

- [ ] Generalize `ActorCritic` accepted decisions with an optional opaque value.
- [ ] Replace GEPA's built-in Mutation machinery with a domain Mutator/generator
      task boundary that returns a complete candidate.
- [ ] Generalize search/evaluation identity and results from string mappings to
      opaque finite JSON candidate values.
- [ ] Move simpletrade request/prompt/envelope extraction and source validation
      into its domain Mutator/Critic implementation.
- [ ] Add regressions for response extraction and filesystem extraction without
      GEPA knowing either transport.
- [ ] Run focused/full Eggopt and simpletrade tests, Ruff, update docs, and commit
      coherent changes with tracked working trees clean.

## Status notes

- 2026-07-30: User clarified the boundary after commits `90fb063` and `185a27c`:
  allowing a domain Critic is insufficient while GEPA still owns `Mutation`,
  strict JSON parsing, component updates, and Actor prompts. Refactor started at
  the stronger opaque-candidate/complete-domain-Mutator boundary.
