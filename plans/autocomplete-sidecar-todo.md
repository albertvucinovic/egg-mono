# Full-History Autocomplete Sidecar — Implementation TODO

**Status:** authorized by user on 2026-07-25; implementation in progress
**Canonical database:** `.egg/threads.sqlite` remains unchanged and authoritative
**Design basis:** `plans/cache-sidecar.md`, narrowed initially to full-history autocomplete

## Non-negotiable requirements

- [x] Preserve autocomplete over the complete canonical effective history; do not impose recent-message semantic cutoffs.
- [x] Keep every match reachable. Bounded response/page rendering may not make older matches inaccessible.
- [x] Preserve current case-insensitive exact/prefix/suffix ID semantics and command-specific chronological ordering.
- [x] Canonical command execution revalidates selected IDs; the sidecar is disposable acceleration only.
- [x] Do not alter the schema or authority of `threads.sqlite`.
- [x] Use a versioned project-local sidecar derived deterministically from the exact canonical DB path.
- [x] Missing, stale, incompatible, corrupt, locked, or unwritable cache state must not corrupt or block canonical writes.
- [x] Egg and EggW consume shared EggThreads sidecar semantics.
- [x] Do not perform total-history work on UI/FastAPI event-loop request paths.
- [x] Publish complete generations only; readers never see partial builds.
- [x] Coordinate builds cross-process and make build/error/watermark state inspectable.
- [x] Preserve lightweight non-record completion while a cold full-history index is preparing.

## Phase 1 — Sidecar foundation and exact catalog

- [x] Finalize semantic v4 path, manifest, schema, permissions, source anchors, and build lease.
- [x] Implement bounded full build from a fixed canonical watermark into an inactive generation.
- [x] Store lightweight effective message/tool record metadata, normalized and reversed IDs, ordering, bounded previews, and pairing metadata.
- [x] Atomically activate a verified complete generation.
- [x] Implement indexed exact/prefix/suffix lookup and complete-history ordered paging.
- [x] Add status/preparing/error result types without silently presenting partial data as complete.
- [x] Add stale-anchor, lock, crash-boundary, and canonical-isolation tests. (Corrupt-file quarantine remains Phase 4.)

## Phase 2 — Incremental correctness

- [x] Apply completion-relevant semantic event tails with a bounded tail size and full-rebuild fallback.
- [x] Preserve create/edit/delete/continue/preserve/tool declaration/result semantics. (Optimizer metadata does not change completion identity/search output.)
- [x] Rebuild or fail closed for unknown semantic versions/events.
- [x] Add differential tests against canonical `list_show_record_candidates()`.

## Phase 3 — Shared consumers

- [x] Route `/show`, generic record IDs, `/editor`, `/continue`, and `/duplicateThread` through the shared sidecar API.
- [x] Preserve full-history content matching with a trigram index rather than recent-message truncation.
- [x] Remove full `snapshot_json` reads/decode from completion hot paths.
- [ ] Make terminal completion cancellation cooperative. (Cold/stale work is no longer on the completion worker, so this remains optional hard-cancel polish.)
- [x] Move EggW sidecar preparation work off the async event loop; queries are bounded indexed reads with existing HTTP stale-request fences.
- [x] Fix thread-selector completion so it does not derive runnability for every thread before filtering.

## Phase 4 — Operations and validation

- [x] Add `/autocompleteCache status|verify|warm|rebuild|clear` with path, version, size, watermark, owner, target, and last error.
- [x] Add real 15k-message/full-history performance evidence and indexed-query-plan assertions.
- [ ] Validate multi-process Egg/EggW sharing, project moves/copies/replacements, deletion, disk-full/read-only, and safe cache removal.
- [x] Run focused and broad EggThreads/Egg/EggW suites.

## Status notes

- 2026-07-25: User explicitly authorized sidecar implementation after confirming that autocomplete must retain complete-history functionality. Repository starts at `be31665`; only unrelated untracked `count-lines.sh` exists. No canonical schema change is authorized. Phase 1 begins with the shared EggThreads foundation and focused tests.
- 2026-07-26: Foundation committed as `8a2eaf0`. Follow-up implementation now uses semantic sidecar v4 with full-history ID/content/term indexes, complete-history cursor paging, incremental semantic tails, shared Egg/EggW consumers, managed cold/stale builders, and user-facing operations. Real 15.6k-message thread: full v4 build ~8.4s in background; one-message semantic catch-up ~1.9s in background; warm `/show` ~26ms, `/continue` ~45ms, ordinary word completion ~65ms. Validation: EggThreads 1691 passed; Egg 697 passed; EggW 265 passed, 1 skipped, one pre-existing warning; compileall and `git diff --check` passed. Remaining follow-up hardening: corrupt-file quarantine, disk-full/read-only/deletion lifecycle, optional cooperative cancellation, broader multi-process fault injection.
