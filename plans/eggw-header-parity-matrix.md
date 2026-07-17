# Egg / EggW Header Parity Matrix

Status: Phase 6 inventory; first bounded EggW message-header slice implemented
Baseline: `c26960e`
Canonical sources inspected: Egg static transcript panels/text, EggW `/messages`
projection and ordered SSE feed, EggW pending-tool API, and shared live lifecycle
events.

## Shared canonical fields

| Object | Terminal Egg header today | EggW before this slice | Canonical/shared source | This slice / remaining gap |
| --- | --- | --- | --- | --- |
| User message | role, local time, full `msg_id`, model when present, content tokens | role; model/tokens except min; time/full ID only max | `/messages`: `id`, `timestamp`, `model_key`, `tokens` from projected `msg.create` + token stats | Implemented one shared EggW header row at max/medium/min. Max shows full ID; medium/min show copyable suffix with full value in title/ARIA. |
| Assistant message | role, local time, full `msg_id`, model, content tokens, TPS | role; model/tokens/TPS except min; time/full ID only max | Same message projection; TPS is durable `msg.create` metadata | Implemented across all verbosity levels through the shared header row. |
| Assistant Note | distinct role, local time, full `msg_id`, model, content tokens, TPS | distinct role; same prior omissions as assistant | Same projection plus `answer_user_preserve_turn` | Implemented through the same shared row; no note-specific authority. |
| Tool declaration / reasoning compact card | terminal titles carry source time/`msg_id`, model, token/TPS metadata; individual call rows carry full `tool_call_id` | medium/max source message header plus detail; min compact card lacked source header; call suffix/title existed after chronology repair | Assistant `msg.create` fields and exact structured call ID | Implemented source message header on min compact cards. Exact full call ID remains in title/inspect body; suffix remains visible. Per-detail token decomposition remains a later presentation question. |
| Durable tool result | name/role, local time, full `msg_id`, model, content tokens, TPS; medium/min also show full call ID | role/name; model/tokens/TPS except min; time/full message and call IDs only max; min had call suffix only | Tool `msg.create`: all listed fields already returned by `/messages` | Implemented shared message fields and copyable message/call IDs at all levels, including the min source-position card. |
| Recovery/error/system record | semantic role (`Continue Status`, `Error`, or `System`), local time, full `msg_id`, model if present | semantic `Continue Status`/`System`; same prior header omissions; runner error still presented as System in EggW | System `msg.create`, including `recovery_notice`, `runner_error`, model, ID/time | Implemented available message fields. **Gap:** EggW API model does not expose `runner_error`, so exact terminal `Error` title parity would require an agreed API/public-field change; not changed here. |
| Compaction marker | boundary label, marker/start event numbers, selected start ID suffix, selector/creator, event time in terminal panel title | boundary label, marker/start event numbers, start suffix, selector/creator; no marker timestamp/full start-ID copy | Marker is synthesized by `/messages`; API model currently omits marker timestamp and has no marker message ID semantics | **Gap:** timestamp is not exposed and full start-ID copy UX is not shared. Documented only; no protocol change. |
| Live LLM/provider | kind/model, TPS, elapsed duration, timeout when known | kind/model, TPS, elapsed/timeout via live timing leaves | Ordered `stream.open`, `provider_request.started`, deltas, stats | Already substantially present; no change in this bounded durable-header slice. Exact invoke/event identity is not currently a user header in either client. |
| Live tool | tool name, elapsed/timeout/summary/state; call identity available from lifecycle | name, call suffix, streaming/finished/wait state, elapsed/timeout, inspectable args/output | Ordered `tool_call.*` and `stream.delta` lifecycle | Existing EggW coverage retained. **Gap:** complete call ID is title/body rather than a dedicated copy control; approval/lifecycle-state terminology differs by client and needs separate semantic review. |
| Pending approval card | tool name, lifecycle/prompt state and exact lifecycle semantics | name, call suffix, `Exec Approval`/`Output Approval`, args/output/summary | `/tools`: ID, name, arguments, state, output, decisions, summary | No change. API lacks durable timestamps/model/duration/token fields for this object; adding them would be protocol work. Output-approval semantics are explicitly out of scope. |
| Local operational/command record | client-generated command/status headers and timing where available | command card/time generated locally; not always durable/cross-client | client operation record, not a canonical `msg.create` in every case | Inventory only. Must not be conflated with durable record parity; needs later shared operational-semantics work. |

## First bounded implementation decision

The coherent available-data group is the durable message header shared by user,
assistant, Assistant Note, tool-result, and system/recovery cards: model, content
tokens, TPS, timestamp, message ID, and tool-call ID where applicable. EggW
already received every field, so the repair is presentation-only. A shared
`MessageHeaderFields` renderer now owns those fields for all verbosity levels and
for min compact detail cards. Min uses compact ID suffixes but preserves full
copy/title/ARIA values; medium/max use the same authority, with max retaining full
visible IDs. No backend/projection/schema/public-protocol changes were needed.
