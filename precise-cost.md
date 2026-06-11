# precise-cost.md

Hierarchical TODO for API-confirmed cache usage and precise cost accounting.

## Research summary

### OpenAI / OpenAI-compatible Chat Completions

- Prompt caching is automatic for supported OpenAI models when prompts are at least 1024 tokens.
- Cache hits require exact prompt-prefix matches. Static content, examples, tools, and schemas should stay at the beginning; volatile/user-specific content should stay at the end.
- OpenAI usage fields for Chat Completions include:
  - `prompt_tokens`
  - `completion_tokens`
  - `total_tokens`
  - `prompt_tokens_details.cached_tokens`
  - `completion_tokens_details.reasoning_tokens` when applicable
- For streaming Chat Completions, OpenAI only sends usage in the stream when `stream_options: {"include_usage": true}` is requested. The final usage chunk may have an empty `choices` array.

### OpenAI Responses API / Codex OAuth provider

- OpenAI Responses usage fields include:
  - `input_tokens`
  - `output_tokens`
  - `total_tokens`
  - `input_tokens_details.cached_tokens`
  - `output_tokens_details.reasoning_tokens` when applicable
- Streaming Responses sends the completed response object on `response.completed` / `response.done`; that response can contain `usage`.
- The `openai-pro` / Codex OAuth provider uses the Responses-style endpoint (`chatgpt.com/backend-api/codex/responses`), so parsing `response.completed.response.usage` is the key path for confirming whether cached tokens are actually being returned.
- OpenAI prompt-cache retention can be requested with `prompt_cache_retention: "24h"` on supported Responses/Chat Completions models. `prompt_cache_key` is a routing-affinity hint and should remain provider-configured.

### Anthropic-style usage, if present from an adapter/provider

- Anthropic prompt-caching usage is shaped differently:
  - `cache_read_input_tokens`: cached tokens read from cache
  - `cache_creation_input_tokens`: tokens written into cache
  - `input_tokens`: uncached/non-cache-breakpoint input tokens, not total logical input
  - `output_tokens`
  - optional `cache_creation.ephemeral_5m_input_tokens` and `cache_creation.ephemeral_1h_input_tokens`
- Total logical input for that shape is:

  ```text
  input_tokens + cache_creation_input_tokens + cache_read_input_tokens
  ```

- Cost can be exact only if the cost config contains enough tiers for cache reads/writes. Fallback should remain safe and explicit.

## Design constraints / invariants

- Keep provider usage parsing opportunistic: if an API returns usage, capture it; if not, preserve the current approximate accounting.
- Do not send local accounting metadata (`api_usage`, raw provider usage, etc.) back to provider APIs on later turns.
- Preserve the current `/cost`, header, and token-stat shapes as much as possible.
- Use actual provider usage for cost when available; use existing heuristic token accounting only as fallback.
- Store raw provider usage only as small assistant-message metadata for audit/debugging.
- Continue to aggregate by model key so model switches still produce correct per-model costs.
- Do not add a new public command or large UI redesign unless needed; `/cost` is enough for detailed confirmation.
- Keep `count-lines.sh` untouched.

## Phase 1 — Provider usage capture

- [x] Add a small normalization helper for provider usage shapes.
  - [x] Normalize OpenAI Chat Completions usage:
    - `prompt_tokens` -> `total_input_tokens`
    - `completion_tokens` -> `total_output_tokens`
    - `prompt_tokens_details.cached_tokens` -> `cached_input_tokens`
    - `completion_tokens_details.reasoning_tokens` -> `total_reasoning_tokens`
  - [x] Normalize OpenAI Responses usage:
    - `input_tokens` -> `total_input_tokens`
    - `output_tokens` -> `total_output_tokens`
    - `input_tokens_details.cached_tokens` -> `cached_input_tokens`
    - `output_tokens_details.reasoning_tokens` -> `total_reasoning_tokens`
  - [x] Normalize Anthropic-style usage if encountered:
    - `input_tokens + cache_creation_input_tokens + cache_read_input_tokens` -> `total_input_tokens`
    - `cache_read_input_tokens` -> `cached_input_tokens`
    - `cache_creation_input_tokens` -> `cache_creation_input_tokens`
    - `output_tokens` -> `total_output_tokens`
- [x] Parse streaming Chat Completions usage chunks, including chunks with empty `choices`.
- [x] Parse Responses `response.completed` / `response.done` usage.
- [x] Attach normalized usage to the final assistant message returned by eggllm.
- [x] Preserve raw provider usage in a small debug/audit field.
- [x] Tests: eggllm adapter/unit tests for OpenAI Chat Completions and Responses usage parsing.

## Phase 2 — Persistence and provider-sanitization

- [x] Ensure `ThreadRunner` persists provider usage metadata on assistant `msg.create` events.
- [x] Strip local usage metadata before future provider requests in both relevant sanitization layers.
- [x] Tests: runner/sanitization tests proving usage metadata is stored locally but not sent back to the provider.

## Phase 3 — Exact token stats and cost calculation

- [x] Update token stats aggregation to prefer per-assistant actual `api_usage` when present.
- [x] Preserve heuristic accounting for assistant messages without actual usage.
- [x] Aggregate actual fields into existing totals:
  - `total_input_tokens`
  - `cached_input_tokens`
  - `cache_creation_input_tokens` when present
  - `total_output_tokens`
  - `total_reasoning_tokens`
  - `by_model`
- [x] Add actual-vs-estimated call counters so `/cost` can show whether totals are API-confirmed.
- [x] Extend cost calculation to handle cache-creation tiers when configured:
  - `cache_creation_input`
  - optional `cache_creation_5m_input`
  - optional `cache_creation_1h_input`
  - fallback cache-creation pricing to normal input if no tier is configured.
- [x] Tests: token-count and cost tests covering exact OpenAI-style cached usage and fallback heuristic usage.

## Phase 4 — Config and visibility

- [x] Request Chat Completions streaming usage for the official OpenAI provider via `models.json` config (`stream_options.include_usage`).
- [x] Ensure the `openai-pro` provider requests `prompt_cache_retention: "24h"` alongside `store: false` and configured `prompt_cache_key` routing.
- [x] Update `/cost` output to show:
  - cached input totals and hit rate;
  - actual/API-confirmed call count vs estimated call count;
  - cache-creation tokens/cost when nonzero.
- [x] Keep the header compact; do not add detailed cache diagnostics there unless already natural.
- [x] Tests: focused command/formatting tests for the new `/cost` lines.

## Phase 5 — Final verification

- [ ] Run focused eggllm tests.
- [ ] Run focused eggthreads token/cost/runner tests.
- [ ] Run any nearby Egg UI/command tests affected by `/cost` display.
- [ ] Update this TODO with final status, commands, results, commits, and caveats.

## Status notes

- 2026-06-11: Created after research. No implementation yet. Current branch had tracked files clean before this file, with only pre-existing untracked `count-lines.sh`.
- 2026-06-11: Phase 1 implemented in eggllm. Normalized usage is stored on final assistant messages as `api_usage`; raw provider usage is stored as `provider_usage`. Focused test run: `python -m pytest eggllm/tests -q` (54 passed). Phase 2 remains persistence and provider-sanitization.
- 2026-06-11: Phase 2 implemented. `ThreadRunner` intentionally preserves usage metadata on local assistant messages and strips `api_usage`/`provider_usage` before provider requests; eggllm client sanitization strips the same fields before sync/async/context-only provider payloads. Focused test runs: `python -m pytest eggthreads/tests/test_usage_metadata_sanitization.py eggthreads/tests/test_reasoning_summary_display_only.py eggthreads/tests/test_tool_message_format.py eggthreads/tests/test_tool_call_id_normalization.py eggthreads/tests/test_toolcall_protocol_enforcement.py eggllm/tests/test_client_sanitize.py -q` (37 passed), `python -m pytest eggllm/tests -q` (57 passed). Phase 3 remains exact token stats/cost aggregation.
- 2026-06-11: Phase 3 implemented. Token stats prefer per-assistant `api_usage` when present, keep heuristic fallback for estimated calls, aggregate `cache_creation_input_tokens` and actual/estimated call counters at top level and per-model, and cost calculation now handles cache-creation tiers with fallback to normal input pricing. Focused tests added in `eggthreads/tests/test_token_count_public.py` and eggllm cost helper coverage. Test runs: `python -m pytest eggthreads/tests/test_token_count_public.py eggthreads/tests/test_command_registry.py eggllm/tests/test_client_sanitize.py -q` (48 passed), `python -m pytest eggllm/tests -q` (58 passed), `python -m pytest eggthreads/tests -q` (615 passed), `python -m pytest egg/tests/test_formatting.py egg/tests/test_commands_utility.py -q` (64 passed). Phase 4 remains config and visibility.
- 2026-06-11: Phase 4 implemented. Packaged/example OpenAI config requests streaming usage, `openai-pro` provider parameters now include `prompt_cache_retention: "24h"` with existing `store: false` and `prompt_cache_key`, and `/cost` shows cached input hit rate, actual/estimated calls, and cache-creation tokens/cost when present. Header was left unchanged. Focused test runs: `python -m pytest eggthreads/tests/test_command_registry.py eggllm/tests/test_client_sanitize.py -q` (38 passed), `python -m pytest egg/tests/test_formatting.py egg/tests/test_commands_utility.py -q` (64 passed). Phase 5 remains final verification.
