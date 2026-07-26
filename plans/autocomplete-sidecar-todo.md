# Full-History Autocomplete Sidecar — Implementation TODO

**Status:** authorized by user on 2026-07-25; implementation in progress  
**Canonical database:** `.egg/threads.sqlite` remains unchanged and authoritative  
**Design basis:** `plans/cache-sidecar.md`, narrowed initially to full-history autocomplete

## Non-negotiable requirements

- [ ] Preserve autocomplete over the complete canonical effective history; do not impose recent-message semantic cutoffs.
- [ ] Keep every match reachable. Bounded response/page rendering may not make older matches inaccessible.
- [ ] Preserve current case-insensitive exact/prefix/suffix ID semantics and command-specific chronological ordering.
- [ ] Canonical command execution revalidates selected IDs; the sidecar is disposable acceleration only.
- [ ] Do not alter the schema or authority of `threads.sqlite`.
- [ ] Use a versioned project-local sidecar derived deterministically from the exact canonical DB path.
- [ ] Missing, stale, incompatible, corrupt, locked, or unwritable cache state must not corrupt or block canonical writes.
- [ ] Egg and EggW consume shared EggThreads sidecar semantics.
- [ ] Do not perform total-history work on UI/FastAPI event-loop request paths.
- [ ] Publish complete generations only; readers never see partial builds.
- [ ] Coordinate builds cross-process and make build/error/watermark state inspectable.
- [ ] Preserve lightweight non-record completion while a cold full-history index is preparing.

## Phase 1 — Sidecar foundation and exact catalog

- [ ] Finalize semantic v1 path, manifest, schema, permissions, source anchors, and build lease.
- [ ] Implement bounded full build from a fixed canonical watermark into an inactive generation.
- [ ] Store lightweight effective message/tool record metadata, normalized and reversed IDs, ordering, bounded previews, and pairing metadata.
- [ ] Atomically activate a verified complete generation.
- [ ] Implement indexed exact/prefix/suffix lookup and complete-history ordered paging.
- [ ] Add status/preparing/error result types without silently presenting partial data as complete.
- [ ] Add corruption, stale-anchor, lock, crash-boundary, and canonical-isolation tests.

## Phase 2 — Incremental correctness

- [ ] Apply completion-relevant semantic event tails in bounded batches.
- [ ] Preserve create/edit/delete/continue/preserve/tool declaration/result/optimizer semantics.
- [ ] Rebuild or fail closed for unknown semantic versions/events.
- [ ] Add differential tests against canonical `list_show_record_candidates()`.

## Phase 3 — Shared consumers

- [ ] Route `/show`, generic record IDs, `/editor`, `/continue`, and `/duplicateThread` through the shared sidecar API.
- [ ] Preserve full-history content matching; add compatible indexed text search rather than recent-message truncation.
- [ ] Remove full `snapshot_json` reads/decode from completion hot paths.
- [ ] Make terminal completion cancellation cooperative.
- [ ] Move EggW sidecar preparation/query work off the async event loop with stale request fences.
- [ ] Fix thread-selector completion so it does not derive runnability for every thread before filtering.

## Phase 4 — Operations and validation

- [ ] Add cache status/verify/warm/rebuild/clear operations with paths, version, size, watermark, owner/progress, and last error.
- [ ] Add real 15k-message/full-history performance fixture and indexed-query-plan assertions.
- [ ] Validate multi-process Egg/EggW sharing, project moves/copies/replacements, deletion, disk-full/read-only, and safe cache removal.
- [ ] Run focused and broad EggThreads/Egg/EggW suites.

## Status notes

- 2026-07-25: User explicitly authorized sidecar implementation after confirming that autocomplete must retain complete-history functionality. Repository starts at `be31665`; only unrelated untracked `count-lines.sh` exists. No canonical schema change is authorized. Phase 1 begins with the shared EggThreads foundation and focused tests.
