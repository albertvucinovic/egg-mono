# NativeGEPA sandbox correction TODO

## Invariants

- A GEPA run has one authoritative private `run/.egg` directory.
- No model-driven thread or its subprocess/REPL tools may read or write its own `.egg`, through relative, absolute, traversal, or symlink paths.
- Mutation retains its current run-root workspace per user decision; do **not** move it to `workspaces/mutation` in this change.
- Mutation retains authorized read-only descendant transcript inspection through Eggthreads APIs and cannot message descendants.
- Sandbox setup fails closed when mandatory metadata protection cannot be established.
- Do not access SQLite/filesystem metadata as a replacement for authorized APIs.

## Work

- [x] Fix Docker sandbox masking at the actual host-bind destination; remove the skipped-mask regression.
- [x] Add real Docker tests for transient Bash and persistent Python REPL, covering relative/absolute/traversal/symlink access and fail-closed setup.
- [x] Move Eggflow `flow.db` to `run/.egg/flow.db` so the run has one private metadata directory.
- [x] Prevent persistent REPL startup from creating nested workspace `.egg` mountpoints.
- [x] Preserve/test Mutation's authorized descendant `python_repl` inspection and messaging restriction.
- [x] Version corrected sandbox/policy cache identities so unsafe cached Mutation results cannot be reused.
- [x] Clean root-owned empty nested `.egg` directories after relevant containers are stopped.
- [x] Run focused/full tests, commit coherent changes, then prepare a fresh run directory (without changing Mutation workspace).

## Status notes

- 2026-07-24: Audit completed without edits. Round 7 was contaminated because Docker mounted the host working directory at `/workspace/host` while masking only `/workspace/.egg`.
- 2026-07-24: User approved all proposed corrections except moving Mutation to a narrow workspace. Round 7 launcher and known Mutation container stopped before implementation.
- 2026-07-24: Docker subprocess and persistent-REPL mounts now use a private parent plus `/workspace/host`, and mask an existing private `.egg` at that actual bind destination. Missing `.egg` paths are not mounted, avoiding nested root-owned mountpoints; unsafe mountpoints fail closed. Real Docker alias tests pass for Bash and Python REPL. Eggflow now stores `flow.db` under run `.egg`; solver-safe profile, REPL mount policy, and NativeGEPA generation identity were versioned. All 12 remaining round-7 session containers were stopped and the 11 empty nested `.egg` directories were removed. Focused suites passed (Eggthreads 59; Eggopt 51). Eggthreads full suite passed 1605 tests excluding an unrelated pre-existing zombie-reaping test file; that isolated test fails because this long-lived launcher does not reap orphan zombies. Eggopt full collection is blocked by the installed upstream `gepa` API mismatch; all locally relevant tests pass.
- 2026-07-24: Committed the core boundary fix and Eggopt private-runtime/cache correction as two focused commits. Fresh launch target is round 8; Mutation intentionally retains the run-root workspace.
- 2026-07-24: Follow-up restored the clean public container layout: the host working directory and container CWD are `/workspace` directly. Egg creates a host `.egg` mountpoint as the current user when absent, then always overlays `/workspace/.egg` with a separate empty read-only mask. This keeps metadata created later during a persistent REPL session hidden without Docker creating root-owned directories. The private-parent `/workspace/host` workaround is retained above only as audit history, not current behavior.
- 2026-07-24: Transient Docker tools and persistent REPLs use the thread database's authoritative `<run>/.egg/sandbox/masks/empty` as their read-only `/workspace/.egg` source; bwrap uses the same canonical path relative to its Egg metadata root. Setup fails closed if the source is not a real, host-user-owned empty directory.
