# Pricing TODO: $/M units + full-thread / since-compaction cost

Goal: standardise all pricing to **dollars per million tokens ($/M)** in
models.json and throughout the cost-calculation code path.  Additionally,
expose cost for the full thread and separately for the period since the
most recent compaction, keeping the per-model breakdown.

## Units migration plan

Every cost value today flows through three layers:

1. **models.json** — stores `cost` as *cents per 1K tokens*.
2. **eggllm current_model_cost_config()** — converts cents/1K → **USD/1K**
   (÷100).
3. **eggthreads token_count._cost_for_usage()** and
   **eggllm approximate_thread_cost()** — convert USD/1K → **USD total**
   via `tokens * (price / 1_000)`.

The desired pipeline is:

1. **models.json** — stores `cost` as *dollars per 1M tokens*.
2. **eggllm current_model_cost_config()** — returns $/1M unchanged.
3. **Cost calculators** — `tokens * (price / 1_000_000)` → USD total.

### Phase 1 – models.json cost values: cents/1K → $/1M

- [ ] Convert every `cost` block in `eggconfig/eggconfig/data/models.json`.
  - Multiply each value by 10 to go from *cents/1K* to *$/1M*.
  - `input_tokens`, `cached_input`, `output_tokens`, `cache_prompt` (fix
    key to `cached_input` where still wrong).
  - Verify against real API pricing pages for major providers (OpenAI,
    Anthropic, Google, DeepSeek, OpenRouter).
  - Update `eggllm/models.json.example` if it documents the cost format.
  - Status notes:

### Phase 2 – eggllm client: remove cents→dollar conversion

- [ ] `current_model_cost_config()` — return the raw $/1M values; drop
  `_cents_to_usd` division.
- [ ] `approximate_thread_cost()` — rename `_usd` helper and change
  denominator from 1000 → 1_000_000.
  - Rename internal variable `price_per_1k` → `price_per_1M`.
- [ ] Update docstrings that mention “cents per 1K”.
- [ ] Run eggllm-focused tests (if any).
  - Status notes:

### Phase 3 – eggthreads token_count: denominator 1_000 → 1_000_000

- [ ] `_cost_for_usage()` — change `_usd` from `tokens * (price / 1000)`
  to `tokens * (price / 1_000_000)`.
  - Rename internal `price_per_1k` → `price_per_1M`.
- [ ] Update docstrings (“USD per 1K” → “USD per 1M”).
- [ ] `_example_cost_cfg_note` — update example message.
- [ ] Run eggthreads token-count tests:
  `python -m pytest eggthreads/tests/test_token_count_public.py -q`
  - Status notes:

### Phase 4 – tests and integration

- [ ] Update any test fixtures that embed cost values to the new $/M
  scale.
- [ ] Run full test suites:
  `python -m pytest eggllm/tests eggthreads/tests egg/tests -q`
- [ ] Manual spot-check: `/cost` output matches expected real-world costs
  for a known model+token-count.
  - Status notes:

## Full-thread vs since-last-compaction cost

Currently `thread_token_stats()` returns one `api_usage` that covers the
full effective history (snapshot + streaming tail).  The user also wants
cost *since the most recent compaction* separately accessible.

The compaction boundary is already tracked:
- `merger._merge_token_stats_with_boundary()` records
  `snapshot_context_tokens`.
- `streaming_token_stats()` computes token stats for events after the
  snapshot (the "streaming tail").
- `thread_token_stats()` has `full_thread_tokens` (pre-compaction) and
  `context_tokens` (post-compaction).

### Phase 5 – per-model cost since last compaction

- [ ] Add `cost_since_compaction` (or a second `api_usage` key) to
  `thread_token_stats()` output.
  - The snapshot-side api_usage represents "up to compaction" cost.
  - The streaming-tail api_usage (from `streaming_token_stats`) is
    "since last compaction".
  - Both need per-model cost attachment via `_attach_costs()`.
- [ ] Call `_attach_costs()` on both the snapshot and streaming
  api_usage structures before returning, storing results under e.g.
  `api_usage.cost_usd` (full) and `api_usage_since_compaction.cost_usd`
  (since last compaction).
- [ ] Update UIs (`egg/egg/panels.py`, `/cost` command, diagnostics) to
  surface both totals when a compaction is present.
  - Status notes:

### Phase 6 – future: API-returned cost

- [ ] (Optional) Research whether provider APIs (OpenAI, Anthropic,
  DeepSeek) include cost/usage in their streaming or final response.
  If yes, add an optional `provider_reported_cost` field that can be
  compared against our estimate.  Out of scope for this initial TODO;
  create a separate feature-request if desired.

