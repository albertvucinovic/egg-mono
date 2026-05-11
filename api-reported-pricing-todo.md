# API-Reported Pricing TODO

Goal: capture token-count / cost information that providers return in their
API responses, store it alongside the thread events, and present it in the UI
as a cross-check against Egg's own estimation.  The estimated cost (from
`models.json` × token counting) remains the primary metric; the
provider-reported figures serve as an optional secondary signal.

## Background — what each major provider returns

### OpenAI (Chat Completions)
- Non-streaming: `response.usage` → `{prompt_tokens, completion_tokens, total_tokens}`.
- Streaming: set `stream_options: {"include_usage": true}` → last chunk before
  `[DONE]` carries an `usage` object with the same shape.
- Does **not** return a USD cost directly.

### Anthropic (Messages)
- Streaming: `message_start` event carries `usage.input_tokens`; `message_delta`
  carries incremental `usage.output_tokens`; `message_stop` / the final
  accumulated Message object has the complete `usage`.
- Non-streaming: `response.usage` with `input_tokens`, `output_tokens`.
- Does **not** return a USD cost directly.

### DeepSeek (OpenAI-compatible Chat Completions)
- Supports `stream_options.include_usage` → usage in last stream chunk.
- Same shape as OpenAI: `usage.{prompt_tokens, completion_tokens, total_tokens}`.
- Does **not** return a USD cost directly.

### Google Gemini (generateContent)
- Non-streaming: `response.usageMetadata` → `{promptTokenCount, candidatesTokenCount, totalTokenCount}`.
- Streaming: same `usageMetadata` available on the final aggregated response.
- Does **not** return a USD cost directly.

### OpenRouter
- **Returns cost directly**: every streaming / non-streaming response includes a
  `usage` field *and* a `cost` / `total_cost` field (USD).  This is the only
  major gateway that provides actual billing-cost numbers.
- OpenRouter also normalises upstream provider usage into a single shape.

### Other OpenAI-compatible providers
- Many support `stream_options.include_usage` (e.g. Together, Groq, Baseten).
- Shape usually matches OpenAI: `usage.{prompt_tokens, completion_tokens, total_tokens}`.

---

## Overall design

1. **Capture** — every LLM invoke that produces an assistant message or
   streaming content should optionally capture the provider's reported usage
   (and cost when available, e.g. OpenRouter) at the end of the turn.

2. **Store** — provider-reported usage is stored as a new event type
   (`api.usage` or `provider.usage`) under the invoke, or attached as metadata
   to the final `msg.create` / `stream.close` event.

3. **Compute** — `eggthreads.token_count` should ingest the new events and
   produce a `provider_reported_usage` block (parallel to the estimated
   `api_usage` block) in `thread_token_stats()`.

4. **Present** — the Egg UI (`/cost`, diagnostics, Chat Messages header) should
   show the provider-reported token counts and cost alongside the estimated
   ones.  When they diverge significantly, flag it.

---

## Phases

### Phase 1 — Capture provider usage in eggllm providers

- [ ] **OpenAI / OpenAI-compatible providers** (eggllm OpenAI provider,
  openrouter, deepseek, baseten, groq, togetherai, etc.)
  - Set `stream_options.include_usage: true` in streaming requests.
  - On the final stream chunk (`[DONE]` or the chunk with `usage`), extract
    `usage.{prompt_tokens, completion_tokens, total_tokens}`.
  - For non-streaming, extract `response.usage`.
  - OpenRouter additionally extracts `cost` / `total_cost`.
  - Return the captured data from `create_chat_completion()` / the streaming
    generator, perhaps as an extra field on the result object or a separate
    `get_last_usage()` method.

- [ ] **Anthropic provider** (eggllm Anthropic / messages provider)
  - Capture `message_start.usage.input_tokens` and final accumulated
    `output_tokens` from the stream.
  - Return captured data.

- [ ] **Google Gemini provider** (if present)
  - Capture `usageMetadata` from the final response.
  - Return captured data.

- [ ] Define a common shape for provider-reported usage that eggthreads can
  consume, e.g.:
  ```python
  {
    "provider": "openrouter",
    "model": "deepseek/deepseek-v4-pro",
    "input_tokens": 1234,
    "output_tokens": 567,
    "total_tokens": 1801,
    "cost_usd": 0.0034,       # only when provider reports it (OpenRouter)
    "cost_currency": "USD",
  }
  ```
  - Status notes:

### Phase 2 — Store provider usage as events

- [ ] Add a new event type, e.g. `provider.usage`, emitted by the runner
  (`eggthreads/eggthreads/runner.py`) at the end of each LLM turn when the
  provider returned usage data.

- [ ] The `provider.usage` event payload stores:
  - `invoke_id` — which invoke this usage belongs to.
  - `input_tokens`, `output_tokens` (optional `total_tokens`, `cached_input_tokens`).
  - `cost_usd` (optional, OpenRouter).
  - `provider` and `model` for attribution.

- [ ] Emit the event in the runner's LLM call path, right after the final
  assistant message is created or after `stream.close`.

- [ ] Wire eggllm's captured usage through the runner → `provider.usage` event.
  - Status notes:

### Phase 3 — Compute provider-reported cost in token_count

- [ ] In `eggthreads/eggthreads/token_count.py` (or a new sibling module),
  add a function `provider_reported_usage_stats(db, thread_id)` that:
  - Scans `provider.usage` events for the thread.
  - Aggregates per-model and for the full thread.
  - Returns a structure parallel to `api_usage` but from provider data.

- [ ] In `thread_token_stats()`, attach `provider_reported_usage` (full
  thread) and `provider_reported_usage_since_compaction` (since last snapshot)
  alongside the estimated `api_usage`.

- [ ] When provider reports `cost_usd` directly (OpenRouter), store it in the
  provider_reported_usage block without re-computing.

- [ ] Add a small cross-check helper: compare estimated cost vs provider-reported
  cost and flag significant discrepancies (>20% or >$0.01 absolute difference).
  - Status notes:

### Phase 4 — UI presentation

- [ ] Update `/cost` command (diagnostics plugin or `egg/egg/commands/`) to
  show both estimated and provider-reported cost, side by side.

- [ ] Update Chat Messages header (`update_panels()`) to optionally show
  provider-reported cost when available.

- [ ] Add a subtle indicator when provider-reported cost diverges significantly
  from the estimate (e.g. a `⚠` or dim warning).

- [ ] Ensure inline and full-screen modes both benefit.
  - Status notes:

### Phase 5 — Tests and edge cases

- [ ] Unit tests for provider usage capture in eggllm (mock API responses).
- [ ] Unit tests for `provider.usage` event emission in the runner.
- [ ] Unit tests for `provider_reported_usage_stats` aggregation.
- [ ] Integration test: end-to-end from mock LLM response → event →
  thread_token_stats → UI.
- [ ] Edge cases: provider returns partial usage (e.g. only input tokens),
  streaming interrupted mid-way (no usage chunk), non-streaming fallback,
  provider returns usage in a different shape than expected.
  - Status notes:

### Phase 6 — OpenRouter cost as primary? (separate decision)

- [ ] Decide whether OpenRouter's `total_cost` should become the **primary**
  cost display (replacing the estimate) for threads that use OpenRouter
  exclusively.  This is a product decision, not purely technical.
  - If yes: add a flag or preference to prefer provider cost over estimate.
  - If no: keep estimate as primary, show provider cost as secondary.
  - Status notes:

