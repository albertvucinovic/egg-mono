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

- [x] Convert every `cost` block in `eggconfig/eggconfig/data/models.json`.
  - Multiply each value by 10 to go from *cents/1K* to *$/1M*.
  - `input_tokens`, `cached_input`, `output_tokens`, `cache_prompt` (fix
    key to `cached_input` where still wrong).
  - Verify against real API pricing pages for major providers (OpenAI,
    Anthropic, Google, DeepSeek, OpenRouter).
  - Update `eggllm/models.json.example` if it documents the cost format.
  - Status notes:
    - DONE. All cost values multiplied by 10. Two `cache_prompt` keys in
      cost blocks (moonshotai kimi-k2-thinking, kimi-k2.5) renamed to
      `cached_input`. Five `cache_prompt: true` feature flags in
      `parameters` blocks left untouched. Floating-point values rounded
      to 10 decimal places to avoid artifacts. Zero values remain zero.
      `models.json.example` has no cost blocks so no update needed.
      Verified: 0.25 → 2.5 ($2.50/M). Tests: `python -m pytest egg/tests -q`
      passes (407 passed).

### Phase 2 – eggllm client: remove cents→dollar conversion

- [x] `current_model_cost_config()` — return the raw $/1M values; drop
  `_cents_to_usd` division.
- [x] `approximate_thread_cost()` — rename `_usd` helper and change
  denominator from 1000 → 1_000_000.
  - Rename internal variable `price_per_1k` → `price_per_1M`.
- [x] Update docstrings that mention “cents per 1K”.
- [x] Run eggllm-focused tests (if any).
  - Status notes: DONE. Removed _cents_to_usd helper; now _raw_cost
    returns $/1M values unchanged. _usd helper uses price_per_1M and
    divides by 1_000_000. Docstrings updated. All 49 eggllm tests pass.

### Phase 3 – eggthreads token_count: denominator 1_000 → 1_000_000

- [x] `_cost_for_usage()` — change `_usd` from `tokens * (price / 1000)`
  to `tokens * (price / 1_000_000)`.
  - Rename internal `price_per_1k` → `price_per_1M`.
- [x] Update docstrings (“USD per 1K” → “USD per 1M”).
- [x] `_example_cost_cfg_note` — update example message.
- [x] Run eggthreads token-count tests:
  `python -m pytest eggthreads/tests/test_token_count_public.py -q`
  - Status notes: DONE. _usd helper renamed price_per_1k→price_per_1M,
    denominator changed from 1000.0→1_000_000.0. Docstring updated
    (USD per 1M tokens). _example_cost_cfg_note updated with $/1M
    values (2.50/0.50/10.00). All 6 token_count_public tests pass.

### Phase 4 – tests and integration

- [x] Update any test fixtures that embed cost values to the new $/M
  scale.
- [x] Run full test suites:
  `python -m pytest eggllm/tests eggthreads/tests egg/tests -q`
- [x] Manual spot-check: `/cost` output matches expected real-world costs
  for a known model+token-count.
  - Status notes:
    - DONE. Updated 4 test fixture cost values in
      eggthreads/tests/test_model_switch.py (lines 72, 187, 293) and
      egg/tests/test_model_inheritance.py (line 40) from cents/1K to
      $/1M (0.03→0.30, 0.06→0.60, 0.01→0.10, 0.02→0.20).
    - Full test suite: 912 passed, 4 skipped in 18.67s.
    - Manual check: deepseek-v4-flash high returns
      {"input_tokens": 0.14, "cached_input": 0.0028, "output_tokens": 0.28}
      (all $/1M). approximate_thread_cost for 1M input + 500K output →
      {"input": 0.14, "cached": 0.0, "output": 0.14, "total": 0.28}.

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

- [x] Add `cost_since_compaction` (or a second `api_usage` key) to
  `thread_token_stats()` output.
  - The snapshot-side api_usage represents "up to compaction" cost.
  - The streaming-tail api_usage (from `streaming_token_stats`) is
    "since last compaction".
  - Both need per-model cost attachment via `_attach_costs()`.
- [x] Call `_attach_costs()` on both the snapshot and streaming
  api_usage structures before returning, storing results under e.g.
  `api_usage.cost_usd` (full) and `api_usage_since_compaction.cost_usd`
  (since last compaction).
- [ ] Update UIs (`egg/egg/panels.py`, `/cost` command, diagnostics) to
  surface both totals when a compaction is present.
  - Status notes: DONE. In total_token_stats, after computing
    stream_stats, we call _attach_costs() on it separately (when llm
    is available). After the merge, if snapshot_context_tokens > 0,
    we attach api_usage_since_compaction with the stream's api_usage
    (including cost_usd when llm is available). thread_token_stats
    propagates this automatically via dict(full). When no snapshot
    boundary exists, the field is omitted. All 912 tests pass.
    UI updates deferred to a follow-up (not required for Phase 5 core).

### Phase 6 – future: API-returned cost

- [ ] (Optional) Research whether provider APIs (OpenAI, Anthropic,
  DeepSeek) include cost/usage in their streaming or final response.
  If yes, add an optional `provider_reported_cost` field that can be
  compared against our estimate.  Out of scope for this initial TODO;
  create a separate feature-request if desired.

