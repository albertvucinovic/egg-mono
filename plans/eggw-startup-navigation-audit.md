# EggW startup/navigation audit and decision proposal

Status: audit complete; product decision required before implementation

Date: 2026-07-17

Audited baseline: `0272522` (`Synchronize models across Egg clients`)

Scope: Phase 8 startup/navigation only; this document changes no runtime behavior

## Executive finding

EggW's backend startup is read/setup-only with respect to thread data, but every
browser visit to the frontend route `/` performs an eager
`POST /api/threads {"claim_quick_start": true}`. That request creates a real
root, appends the root's canonical initial events, starts a scheduler for the new
root, and only then claims any launcher draft or attachment. Therefore ordinary
browser launch, manual `/`, and each tab opened at `/` create canonical history
even when the user does nothing.

Reloading or directly opening a valid `/{threadId}` does **not** create a thread.
An invalid or deleted direct URL redirects to `/`, which then does create one.
The launcher warm-up fetches `/` as HTML but does not execute the client effect;
the subsequently opened browser does. A `/reload` handoff is intended to reopen
the current direct URL and suppress quick-start reuse, but falling back to `/`
(for example, if that direct target is missing) resumes eager creation.

## Evidence and current authority

- Launcher: `eggw/eggw.sh:18-25` serializes argv into a backend-only,
  claim-once payload; `:208-218` warms frontend assets without running browser
  JavaScript; `:459-465` opens the frontend `/` URL. Backend lifespan
  (`eggw/eggw/main.py:25-73`) initializes schema/config/client only; it does not
  create a thread or scheduler.
- Landing route: `eggw/frontend/src/app/page.tsx:14-25` runs one create request
  per mounted page and redirects to its returned ID. The ref prevents duplicate
  work only within that component instance, not across tabs, remounts, or
  independent requests.
- Creation boundary: `eggw/eggw/routes/threads.py:152-224` calls
  `create_root_thread`, appends the root system prompt, calls
  `ensure_scheduler_for`, and then claims quick-start state. The API has no
  startup request key/idempotency key.
- Canonical effects: `eggthreads/eggthreads/api.py:312-344` inserts a ULID root
  and normally appends initial `model.switch`; `eggw/eggw/system_prompt.py:68-76`
  appends `msg.create` for the system prompt and publishes a snapshot. With a
  configured default model, one untouched landing visit therefore creates one
  thread and at least those two events. This audit also reproduced the helper
  path locally: default-model creation produced `model.switch` then
  system-message `msg.create`.
- Quick start: `eggw/eggw/core/state.py:101-129` provides a process-local locked
  first-claim-wins payload. `eggw/eggw/routes/threads.py:195-211` claims it only
  **after** allocating the thread. Text is returned as an unsent frontend draft;
  a sole existing file is immediately copied to a thread-owned durable input
  namespace before it is returned as a staged attachment. Neither is a user
  transcript message yet.
- Direct route: `eggw/frontend/src/app/[threadId]/page.tsx:65-93` opens and reads
  the routed ID. `POST /{threadId}/open` (`eggw/eggw/routes/messages.py:686-709`)
  starts/reuses a scheduler for the existing root but appends no event. Missing
  routes redirect to `/`; both `open` and the independent thread query can cause
  that redirect.
- Existing-root order: `GET /api/threads/roots` returns roots sorted oldest to
  newest by `(latest event_seq, created_at, thread_id)`
  (`eggw/eggw/routes/threads.py:95-128`), so the final item is the current
  activity-based candidate. It intentionally includes legacy unparented
  `@runtime:*` rows so they stay inspectable; a startup selector must not hide or
  silently choose such rows without an explicit eligibility rule.
- Explicit creation remains separate: Ctrl/Cmd+N
  (`eggw/frontend/src/app/[threadId]/page.tsx:256-263`), `/newThread`
  (`eggw/eggw/commands/thread.py:106-127`), and the currently unmounted
  `ThreadTree` New Thread action eagerly create roots. These are intentional user
  actions, unlike landing.

Focused existing characterization passed during this audit: five backend tests
for claim-once draft/file behavior, reload suppression, activity ordering, and
root system-prompt creation; and the browser quick-start draft/attachment test.
No new test was needed to document behavior already covered literally.

## Path-by-path reproduction matrix

| Entry/path | Current canonical mutation | Scheduler effect | Notes |
|---|---|---|---|
| Backend process start, empty or existing DB | Schema `CREATE IF NOT EXISTS` only; no thread/event | None | Configuration and quick-start argv are loaded into process memory. |
| `eggw.sh` frontend warm-up | None | None | HTTP GET compiles `/` and a fake dynamic route; curl does not hydrate React. |
| Browser launch at `/` | **Creates one new root and initial events** | Starts scheduler for that new root | Same result for empty and existing DB; existing roots are ignored. |
| Manual navigation to `/` | **Creates one new root per mount** | Starts its scheduler | Back/forward or any path returning to `/` can repeat it. |
| Browser reload on valid `/{threadId}` | None | Reopens/ensures scheduler for existing root | Transcript/SSE reads are non-creating. |
| Direct valid root or child URL | None | Ensures scheduler for the target's actual root | Child preserves descendant-tree authority. |
| Direct invalid/deleted URL | No mutation on failed reads; then redirect to `/` **creates a new root** | New-root scheduler | Error redirect can fire from both `open` and thread metadata paths. |
| `/reload` command/restart | Intended direct reopen; no new root | Recreates scheduler process-locally for existing root | Launcher suppresses argv; missing target falls through to invalid-route behavior above. |
| Two tabs opened at `/` | **Two roots/events** | One scheduler per distinct new root | `didInitialize` is tab/component-local. |
| Concurrent landing requests | **One root per successful POST** | One per distinct root | ULIDs prevent ordinary collision, but there is no request dedupe/idempotency contract. Quick-start goes only to lock winner. |
| Quick-start text at `/` | **Creates a root**, returns text as unsent draft | Starts scheduler | Only the first claim receives text; losing tabs still create empty roots. |
| Quick-start sole file at `/` | **Creates a root and durable thread-owned input artifact**, no user message | Starts scheduler | Thread identity is required before current attachment staging can persist bytes. |
| Explicit Ctrl/Cmd+N, `/newThread`, API create | **Creates a root and initial events by design** | Starts scheduler | Must remain eager under every proposed landing policy. |

## Required contract independent of option

Any implementation should satisfy all of these:

1. A passive backend start/restart, warm-up, browser launch, ordinary `/`, or
   invalid-route recovery must not create a canonical thread/event or scheduler.
2. A valid direct route remains exact; do not replace it with a recent thread.
   Opening it may start the scheduler required by the existing visited-root
   invariant, but reads must not append lifecycle/configuration events.
3. Explicit New Thread remains eager and visibly distinct from passive landing.
4. Quick-start text/file has one atomic-enough claim owner. Losing tabs must not
   create empty roots, and a failed claimant must not consume an unrecoverable
   launch request. Attachment bytes must not be assigned to a fake owner.
5. The user can reach every root, including legacy orphan runtime rows, without
   silently treating such rows as the preferred chat. Selection must be bounded
   for very large databases and deterministic under concurrent activity/deletion.
6. Creation-on-send must be retry/idempotency safe. One user action must not
   allocate two roots after a timeout/retry, and the first durable user message
   must target the one returned root before its scheduler runs. Existing frontend
   `client_operation_id` values are optimistic-cache identities only and are not
   sent as a backend create/send idempotency contract.
7. A neutral/draft state is not a thread: it cannot run thread commands, start
   SSE, own descendants, use thread-scoped attachment storage, export artifacts,
   or claim ancestor/descendant access until a real root exists.

## Decision matrix

Ratings are relative: **good**, **mixed**, or **poor**.

| Option | Passive start | UX/continuity | Concurrency | API/UI work | Authority and migration risk |
|---|---|---|---|---|---|
| **A. Neutral non-thread chooser/landing** | **Good:** no write | **Good** transparency and explicit choice; one extra click for existing work | **Good** if read-only | New landing shell/list/open/new actions; no schema change | **Low** if it never impersonates a thread. Must expose legacy roots safely and bound the list. |
| **B. Open appropriate existing/recent root** | **Good:** no write when one exists; needs A or C for empty DB | **Good** fast continuity, but automatic context selection can surprise users | **Mixed:** candidate can be deleted or become stale; fetch then exact-route/revalidate | Reuse/extend roots query; define eligibility and “recent” precisely | **Medium:** activity order exists, but no persisted “last opened by this browser” authority; orphan runtime rows complicate auto-selection. |
| **C. Lazy-create on first prompt/attachment** | **Good:** no write until intent | **Good** chat-first flow; draft can look like a conversation before identity exists | **Poor unless redesigned:** create+first-send and retry need a request key/claim protocol | Largest change: neutral composer, create-and-send boundary, draft ownership, upload flow, command gating | **Medium/high:** no schema is necessarily required, but API idempotency and thread-owned attachment semantics must be designed. |
| **D. Synthetic all-zero thread** | **Poor** if persisted (still canonical junk); superficially write-free only if special-cased everywhere | **Mixed/poor:** appears to be a thread but lacks honest ownership | **Poor:** all tabs share one identity/draft/artifact/scheduler namespace | Broad special cases across routes, commands, queries, SSE, exports, and scheduler | **High:** collides with the 26-character Crockford alphabet/ULID shape, sorts as epoch zero, can occupy the PK permanently, and invites FK/tree/access leakage. |

### Option A — neutral chooser

A is the safest standalone foundation. `/` can fetch a bounded root summary,
show Open recent / Browse roots / New Thread, and show a clear empty-database
state. It does not need a database migration and keeps thread-only components
unmounted until selection. The main UX cost is a selection step. The current
`ThreadTree` code is not mounted anywhere, so a real chooser/navigation surface
would need to be introduced rather than assuming the existing component is
visible.

The chooser should not use unrestricted `GET /api/threads` for huge databases.
It needs a bounded roots contract (or a bounded first page) with an explicit
activity definition. A candidate must be revalidated by the existing direct
route; if deleted concurrently, return to neutral with a non-destructive notice.

### Option B — open recent/existing

B can be layered on A: if a clearly eligible recent chat root exists, `/` can
redirect to it; otherwise render neutral. The server already sorts roots by
latest event sequence, but this means “most recently active in the shared DB,”
not “last opened in this tab/device.” Reads/opening do not update a durable
last-opened marker, and adding one would itself make passive viewing a canonical
write. Browser-local remembered URLs would be client state, not shared authority.

Automatic B also risks selecting a legacy orphan runtime root because those are
intentionally visible. If B is chosen, define an eligibility predicate without
hiding records from the chooser. A conservative policy is: auto-open only a
previously explicit browser-local valid route, or do not auto-open at all; show
the shared activity-ordered candidate as an **Open recent** action instead.

### Option C — lazy creation

C best matches “a blank chat that becomes real on send,” but current APIs are
thread-first: send, autocomplete, commands, upload, model/settings, transcript,
SSE, and artifacts all require a real ID. Text can remain in a neutral frontend
draft until send, then an API can allocate the root and append the first user
message under one operation. A two-call `create` then `send` implementation is
not sufficient: timeout/retry can create duplicates and a scheduler starts at
current create time before the first message is attached.

Attachment-only lazy creation is harder. Current upload immediately persists
bytes under a thread-owned directory and embeds `owner_thread_id`; therefore
staging before a root requires either (1) keeping browser `File` objects only in
memory until an atomic claim/upload, or (2) a new expiring upload-ticket authority
with later transfer. The latter is a new persistence/access design and should be
a separate approved proposal. Quick-start local files are backend-known and
claim-once, but still need a real owner before current staging can run.

Commands in a neutral draft also need policy: thread-independent help/selection
could be local/landing actions, while `/model`, `/attach`, `/newThread`, shell,
and all lifecycle commands must either explicitly create or be disabled with a
clear explanation. Silent creation by arbitrary commands would recreate the
startup ambiguity.

### Option D — synthetic `00000000000000000000000000`

Reject D. SQLite accepts the text and `0` is in Crockford Base32, so format-only
checks would not reliably distinguish it. But generated ULIDs encode time and
randomness; a reserved all-zero ID is outside normal allocation semantics and
would sort before real ULIDs. Persisting it creates exactly the unwanted
canonical row/events, while not persisting it breaks every FK-backed event/open
stream and every endpoint that first calls `get_thread`.

If persisted, it becomes one global root and scheduler key; children attach to a
fake ancestor; commands and `/newThread` audit events gain a misleading source;
thread-owned input/output namespaces and access checks share one owner across
users/tabs; duplicate/export/delete operations treat it as real; and deletion or
name/config/model changes become global landing-state mutations. Avoiding those
outcomes requires special-casing nearly every layer and likely migration/repair
rules. A plain neutral state provides all intended benefits without corrupting
identity semantics.

## Recommended product contract

Recommend a deliberate **A + optional C hybrid**, with B presented rather than
silently selected:

1. `/` is a neutral, non-thread landing state. It performs only a bounded read.
2. Show the shared activity-ordered candidate as **Open recent** (with any
   chat-eligibility policy made explicit), plus a bounded root chooser and an
   explicit **New Thread** action. Do not auto-open a
   shared recent root in the first implementation; direct URLs already provide
   exact continuity and avoid surprising cross-client context switches.
3. If there is no quick-start payload, start with A only. Lazy free-text creation
   can follow after an idempotent create-and-send API contract is designed; it is
   not necessary to stop passive startup writes.
4. If launcher text exists, render it as a neutral unsent draft and create exactly
   once on Send/New Thread. This requires splitting “inspect/claim quick start”
   from current create, or one atomic claim-and-create operation with a stable
   launch/request token. Do not consume it through a passive roots read.
5. For launcher files, initially use an explicit **Create thread and stage file**
   action (or eager creation explicitly attributable to the non-empty launch
   request, if the user chooses that policy). Do not invent pre-thread artifact
   ownership. Browser uploads on neutral can remain in-memory until creation.
6. Keep explicit New Thread eager. After creation, route to the real ULID and use
   all existing per-thread APIs unchanged.

This hybrid fixes the reported passive-write bug with the smallest authority
change (A), retains convenient existing-thread access without guessing (B as a
button), and leaves C's valuable chat-first UX behind a proper idempotency and
attachment-ownership design. D should be closed as rejected.

## Implementation/test impact after approval

No schema migration is required for A or A+B. Likely implementation slices:

1. **Read-only landing:** replace Home's create effect with a neutral component;
   add/reuse a bounded roots endpoint and route actions; make invalid/deleted
   routes return neutral without creation. Characterize empty/existing DB, `/`,
   valid/invalid/deleted direct route, reload/restart, and two tabs. Assert thread
   and event counts remain unchanged until explicit New Thread.
2. **Quick-start claim contract:** add a non-consuming status or tokenized claim
   boundary; cover text, sole file, two tabs, claim failure/retry, process restart,
   and `/reload`. This may be combined with explicit create, but must not be a
   passive GET side effect.
3. **Optional lazy draft claim:** add a stable client operation ID and an
   idempotent backend operation that creates a root plus initial user message (and
   only then schedules it). Define in-memory upload handling or separately
   approve a pre-thread upload-ticket design. Test duplicate requests and lost
   responses literally.
4. Preserve direct-route/scheduler, descendant-tree, command, export/artifact,
   access, and explicit New Thread behavior with focused and full regression
   suites. No all-zero compatibility path or migration should be introduced.

## Decision requested from the user

Please choose the startup contract before implementation:

1. **Recommended:** neutral `/` with **Open recent**, bounded chooser, and explicit
   **New Thread**; then separately add lazy create-on-send once idempotency is
   designed. Quick-start text is held as an unsent neutral draft; a quick-start
   file asks to create a real thread before staging.
2. Neutral `/` but automatically open the most recently active eligible root when
   one exists; specify whether shared DB activity or browser-local last-opened
   history should define “recent.”
3. Full lazy blank-chat behavior now, accepting the larger atomic create/send and
   attachment-staging design slice.

Also confirm whether a non-empty launcher quick-start should (a) wait on neutral
for explicit Send/Create (**recommended, no passive creation**) or (b) eagerly
create exactly one real thread because invoking EggW with text/file itself counts
as explicit creation intent. The all-zero synthetic thread is not recommended.
