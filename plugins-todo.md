# Egg Plugin Architecture TODO

This file is the handoff document for implementing Egg's plugin architecture over many sessions.

## Session operating instructions

Every session working on this plan should:

1. Read this file first.
2. Run `git status --short` before editing.
3. Pick the next unchecked item in the earliest incomplete phase, unless the user explicitly asks for another item.
4. Keep each change small, local, and committable.
5. Prefer internal/built-in plugin seams first; external plugin discovery comes later.
6. Preserve core safety invariants. Plugins may provide strategies/providers/policies, but core writes authoritative events and enforces hard boundaries.
7. Run focused tests for the touched area.
8. After each committable change:
   - update this file so the next session can continue without guessing;
   - include any new notes, decisions, or test commands under the relevant task;
   - `git add` the changed files, including this file;
   - `git commit` the unit of work.
9. Do not batch unrelated phases into one commit.
10. If a task appears to require a broad refactor beyond the current phase, stop and ask the user before proceeding.

## Core design principle

Keep mechanisms and invariants in core; make strategies pluggable.

| Area | Core owns | Plugins own |
|---|---|---|
| Tools | registry protocol, approval integration, execution lifecycle, event publication | concrete tools and tool bundles |
| Commands | command registry, dispatch, result protocol, help/autocomplete integration | concrete slash commands and input-prefix handlers |
| Sandbox | requirement enforcement, config inheritance, audit, fail-closed semantics | Docker/SRT/bwrap/containerd/VM provider implementations |
| Sessions | runtime-thread model, eval-token auth, bridge protocol, audit | Docker/memory/containerd/VM/remote session providers |
| Approval | TC state machine, final decision aggregation, authoritative events | approval policies/verdict providers, including small-LLM approvers |
| Output | tool-result lifecycle, channel separation, hard limits, audit | redaction, summarization, truncation, DLP, publication policies |
| Context/memory | transcript/event truth, token-budget accounting, provenance, privacy flags | compaction, summarization, retrieval, memory extraction/storage |

## Plugin bundles and shared implementation rule

Plugins are expected to register multiple related extension types at the same time. A plugin should be a feature bundle, not only a tool bundle or only a command bundle.

Examples:

- `subagents` plugin:
  - tools: `spawn_agent`, `spawn_agent_auto`, `wait`, `send_message_to_child`, `continue_subthread`, `get_child_status`;
  - commands: `/spawnChildThread`, `/spawnAutoApprovedChildThread`, `/waitForThreads`, `/listChildren`, `/parentThread`, `/continue` where appropriate;
  - autocomplete providers for thread selectors;
  - shared service functions for spawning, waiting, child-status formatting, and message sending.
- `session` plugin:
  - tools: REPL/session tools;
  - commands: session and REPL commands;
  - session providers;
  - runtime `eggtools` wrapper contributions.
- `web` plugin:
  - tools: `web_search`, `fetch_url`;
  - commands: `/startSearxng`, `/stopSearxng`;
  - backend/provider config and status helpers.
- `skills` plugin:
  - tool: `skill`;
  - commands: `/skills`, `/skill`;
  - system-prompt/help contribution advertising available skills.

Implementation rule:

- Do not implement the same behavior once for a tool and again for a command.
- Put feature logic in a shared service/module function, then make tool handlers and command handlers thin adapters around it.
- Tool handlers adapt model/JSON arguments plus `ToolContext` into the shared service.
- Command handlers adapt user text plus `CommandContext` into the same shared service.
- Tests should cover the shared service directly where possible, plus one thin integration test for the tool path and one for the command path.
- If a command and tool intentionally differ, document the boundary in the plugin module and in this TODO.

Useful plugin contribution types to support together:

- tools;
- slash commands;
- input-prefix handlers, such as `$` and `$$`;
- autocomplete providers;
- help/system-prompt snippets;
- sandbox providers;
- session providers;
- approval policies;
- output policies;
- context compaction policies;
- memory providers;
- event observers/hooks;
- artifact renderers or artifact storage providers, if output policies need them later;
- status/diagnostic panels or status-line contributions for frontends, if UI plugins need them later.

Core should make it easy for one plugin to register all of these from one `register(plugin_context)` call, while still keeping each registry independent and testable.

## Target plugin interfaces

These are target shapes, not a requirement to implement all at once.

```python
class EggPlugin:
    name: str
    version: str

    def register(self, context): ...  # optional convenience for registering a whole feature bundle
    def register_tools(self, registry): ...
    def register_commands(self, registry): ...
    def register_input_handlers(self, registry): ...
    def register_autocomplete(self, registry): ...
    def register_help(self, registry): ...
    def register_sandbox_providers(self, registry): ...
    def register_session_providers(self, registry): ...
    def register_approval_policies(self, registry): ...
    def register_output_policies(self, registry): ...
    def register_context_policies(self, registry): ...
    def register_memory_providers(self, registry): ...
    def register_hooks(self, registry): ...
```

Important rule: plugin callbacks can propose or implement behavior, but core remains the final arbiter for security-sensitive state transitions.

The preferred API may become a single `plugin.register(context)` entry point where `context` exposes all registries. The per-registry methods above are still useful as a simple protocol and for tests.

## Phase 0 — Baseline and cleanup

Goal: make sure the codebase starts from a known, committed state.

- [x] Confirm the current default tools no longer include removed placeholder/unsafe tools.
  - Expected removed tools: `replace_between`, `javascript`.
  - Suggested check: instantiate `create_default_tools()` and assert those names are absent.
- [x] Run focused tool-wrapper tests.
  - Suggested: `pytest -q eggthreads/tests/test_repl_dynamic_tool_wrappers.py`.
- [x] Commit the existing removals if they are present but uncommitted.
- [x] Keep `plugins-todo.md` updated with the current status after the commit.

Status notes:
- 2026-05-07: `create_default_tools()` no longer registers `replace_between` or `javascript`.
- 2026-05-07: `pytest -q eggthreads/tests/test_repl_dynamic_tool_wrappers.py` passed.
- 2026-05-07: removal commit already exists as `ffa6814 removing replace_between and javascript tools`.

## Phase 1 — Internal plugin manager skeleton

Goal: introduce plugin-shaped registration without external discovery.

- [x] Add a small plugin manager module.
  - Suggested location: `eggthreads/eggthreads/plugins.py` or package `eggthreads/eggthreads/plugins/`.
  - Responsibilities:
    - store built-in plugin objects;
    - expose deterministic registration order;
    - expose registries for tools first, then later commands/providers/policies;
    - support feature-bundle plugins that can register tools, commands, completions, providers, policies, hooks, and help snippets from one plugin object.
- [x] Add a minimal `EggPlugin` protocol/dataclass.
  - Keep it lightweight; avoid abstract base complexity unless needed.
  - Include plugin metadata and an optional single `register(context)` method so related extension points can be registered together.
  - Keep per-registry methods or helpers for tests and simple plugins.
- [x] Add a minimal `PluginContext` object.
  - Initially it may only expose the tool registry.
  - Design it to grow to command/input/autocomplete/help/provider/policy/hook registries.
- [x] Add a central tool-registry factory.
  - Target name suggestion: `create_tool_registry()`.
  - `create_default_tools()` may remain as compatibility wrapper initially.
- [x] Convert `create_default_tools()` to call the new central factory while preserving behavior.
- [x] Add tests proving the new factory returns the same tool names/specs as before.
- [x] Commit.

Status notes:
- 2026-05-07: Added `eggthreads.plugins` with `EggPlugin`, `PluginContext`, `ToolPluginContext`, `FunctionPlugin`, and `register_plugins()`.
- 2026-05-07: Added `create_tool_registry()` and made `create_default_tools()` a compatibility wrapper around it.
- 2026-05-07: Exported `create_tool_registry` from `eggthreads`.
- 2026-05-07: Added `eggthreads/tests/test_plugin_tool_registry.py`.
- 2026-05-07: Focused tests passed: `pytest -q eggthreads/tests/test_plugin_tool_registry.py eggthreads/tests/test_repl_dynamic_tool_wrappers.py`.
- 2026-05-07: Committed as `9ff2714 Add internal plugin tool registry seam`.

Notes for implementers:
- Do not add entry-point discovery in this phase.
- Do not move every tool yet; first create the seam.
- Do not design tools and commands as unrelated plugin systems. The same plugin must be able to register both, and later phases should migrate related tools/commands into the same plugin package.

## Phase 2 — Split built-in tools into internal plugins

Goal: move concrete tools out of one monolithic `tools.py` while preserving public behavior.

This phase should create feature modules that can later register both tools and commands. Even if only tool registration is wired initially, put shared implementation in a neutral service module inside the plugin so command migration can reuse it.

Suggested built-in tool plugins:

- [x] `skills` plugin
  - Tools: `skill`.
  - Commands later: `/skills`, `/skill`.
  - Shared service: list/search/load skill documents.
- [x] `execution` plugin
  - Tools: `bash`, `python`.
  - Keep runner's special bash path for now if needed.
  - Commands/input later: `$`, `$$` shell command handlers may reuse bash enqueue/execution helpers.
- [x] `session` or `rlm_session` plugin
  - Tools: `python_repl`, `bash_repl`, `session_status`, `session_reset`, `session_stop`.
  - Commands later: `/sessionStatus`, `/sessionOn`, `/sessionOff`, `/sessionStop`, `/sessionReset`, `/sessionCleanup`, `/pythonRepl`, `/bashRepl`.
  - Providers later: memory/Docker/containerd/VM session providers.
  - Shared service: session status formatting, target runtime resolution, REPL tool-call enqueue helpers.
- [x] `subagents` plugin
  - Tools: `spawn_agent`, `spawn_agent_auto`, `wait`, `send_message_to_child`, `continue_subthread`, `get_child_status`.
  - Commands later: `/spawnChildThread`, `/spawnAutoApprovedChildThread`, `/waitForThreads`, possibly `/listChildren`, `/parentThread`, `/continue`.
  - Shared service: spawn child, spawn auto-approved child, wait for children, send child guidance, continue child, format child status.
- [x] `web` plugin
  - Tools: `web_search`, `fetch_url`.
  - Commands later: `/startSearxng`, `/stopSearxng`.
  - Shared service: backend selection/status and SearXNG lifecycle helpers.

Implementation guidance:
- Move one plugin group per commit.
- Preserve tool names and schemas exactly unless a test forces a correction.
- Preserve `create_default_tools()` as compatibility until downstream code is migrated.
- Update REPL wrapper generation to use the central factory, not hardcoded default internals.
- For each group, identify any existing command implementation that duplicates tool behavior and extract a shared service before or during command migration.
- Tool implementations should become thin adapters over the shared service; command implementations should later become thin adapters over the same service.
- Run focused tests after each group.

Suggested tests by group:
- Skills: `pytest -q eggthreads/tests/test_skills_tool.py`.
- Execution/REPL: `pytest -q eggthreads/tests/test_python_repl_tool.py eggthreads/tests/test_bash_repl_tool.py eggthreads/tests/test_repl_bridge.py`.
- Web: `pytest -q eggthreads/tests/test_web_searxng.py eggthreads/tests/test_tavily_tools.py`.
- Subagents: `pytest -q eggthreads/tests/test_spawn_capabilities_session.py eggthreads/tests/test_repl_bridge.py`.

Status notes:
- 2026-05-07: Added `eggthreads.builtin_plugins.skills` with `SkillsPlugin`, `register_skill_tool()`, and shared `render_skill_request()` service.
- 2026-05-07: `create_tool_registry()` registered `SkillsPlugin()` plus the temporary legacy built-in tool registrar at this step.
- 2026-05-07: Removed `skill` registration from the legacy monolithic tool population path.
- 2026-05-07: Focused tests passed: `pytest -q eggthreads/tests/test_plugin_tool_registry.py eggthreads/tests/test_skills_tool.py eggthreads/tests/test_repl_dynamic_tool_wrappers.py`.
- 2026-05-07: Added `eggthreads.builtin_plugins.execution` with `ExecutionPlugin`, shared subprocess helpers, `execute_bash_tool()`, and `execute_python_tool()`.
- 2026-05-07: `create_tool_registry()` registered `ExecutionPlugin()` before the temporary legacy registrar at this step.
- 2026-05-07: Removed `bash` and `python` registration from the legacy monolithic tool population path.
- 2026-05-07: Focused tests passed: `pytest -q eggthreads/tests/test_plugin_tool_registry.py eggthreads/tests/test_repl_bridge.py eggthreads/tests/test_repl_dynamic_tool_wrappers.py eggthreads/tests/test_python_repl_tool.py eggthreads/tests/test_bash_repl_tool.py`.
- 2026-05-07: Added `eggthreads.builtin_plugins.session` with `SessionPlugin`, REPL tool adapters, shared session status formatting, and shared runtime target resolution.
- 2026-05-07: `create_tool_registry()` registered `SessionPlugin()` before the temporary legacy registrar at this step.
- 2026-05-07: Removed `python_repl`, `bash_repl`, `session_status`, `session_reset`, and `session_stop` registration from the legacy monolithic tool population path.
- 2026-05-07: Focused tests passed: `pytest -q eggthreads/tests/test_plugin_tool_registry.py eggthreads/tests/test_python_repl_tool.py eggthreads/tests/test_bash_repl_tool.py eggthreads/tests/test_repl_bridge.py eggthreads/tests/test_repl_dynamic_tool_wrappers.py`.
- 2026-05-07: Added `eggthreads.builtin_plugins.subagents` with `SubagentsPlugin`, shared spawn/session attenuation helpers, child messaging/continue/status helpers, and `wait_tool()`.
- 2026-05-07: `create_tool_registry()` registered `SubagentsPlugin()` before the temporary legacy registrar at this step.
- 2026-05-07: Removed `spawn_agent`, `spawn_agent_auto`, `wait`, `send_message_to_child`, `continue_subthread`, and `get_child_status` registration from the legacy monolithic tool population path.
- 2026-05-07: Focused tests passed: `pytest -q eggthreads/tests/test_plugin_tool_registry.py eggthreads/tests/test_spawn_capabilities_session.py eggthreads/tests/test_repl_bridge.py eggthreads/tests/test_child_status.py eggthreads/tests/test_send_message_to_child.py eggthreads/tests/test_repl_dynamic_tool_wrappers.py`.
- 2026-05-07: Added `eggthreads.builtin_plugins.web` with `WebPlugin`, shared max-results handling, `web_search_tool()`, and `fetch_url_tool()`.
- 2026-05-07: `create_tool_registry()` now registers all built-in tool plugins directly and the temporary legacy registrar was removed.
- 2026-05-07: Removed `web_search` and `fetch_url` registration from the legacy monolithic tool population path.
- 2026-05-07: Focused tests passed: `pytest -q eggthreads/tests/test_plugin_tool_registry.py eggthreads/tests/test_web_searxng.py eggthreads/tests/test_tavily_tools.py eggthreads/tests/test_repl_dynamic_tool_wrappers.py eggthreads/tests/test_spawn_capabilities_session.py eggthreads/tests/test_repl_bridge.py eggthreads/tests/test_child_status.py eggthreads/tests/test_send_message_to_child.py`.

## Phase 3 — Tool execution context and richer tool interface

Goal: remove hidden magic args and support future async/streaming/cancellable plugin tools.

- [x] Introduce `ToolContext`.
  - Suggested fields:
    - `db`
    - `thread_id`
    - `invoke_id`
    - `origin` (`llm`, `user_command`, `repl`, etc.)
    - `initial_model_key`
    - `timeout_sec`
    - `cancel_check`
    - `working_dir`
    - sandbox/session handles or accessors
- [x] Keep backward compatibility for existing `impl(args)` tools while adding context-aware execution.
- [x] Stop injecting new private keys where possible.
  - Legacy private keys may stay temporarily:
    - `_thread_id`
    - `_initial_model_key`
    - `_tool_timeout_sec`
    - `_cancel_check`
    - `_egg_tool_timeout_sec`
- [ ] Add async execution support.
  - A tool may provide sync or async implementation.
- [x] Add streaming/cancellation capability metadata.
- [ ] Move bash execution out of the runner special case once the richer interface can express:
  - live stdout/stderr streaming;
  - timeout summaries;
  - lease-loss cancellation;
  - sandbox provider execution;
  - Docker/container cleanup.
- [ ] Commit after each compatibility-preserving substep.

Status notes:
- 2026-05-07: Added `ToolContext` with db/thread/invoke/origin/model/timeout/cancel/working-dir/raw context fields.
- 2026-05-07: Added `accepts_context=True` registration option; legacy tools still receive only `impl(args)`.
- 2026-05-07: Exported `ToolContext` from `eggthreads`.
- 2026-05-07: Added context-aware tool coverage to `eggthreads/tests/test_plugin_tool_registry.py`.
- 2026-05-07: Focused tests passed: `pytest -q eggthreads/tests/test_plugin_tool_registry.py eggthreads/tests/test_repl_dynamic_tool_wrappers.py eggthreads/tests/test_repl_bridge.py`.
- 2026-05-07: Context-aware tools no longer receive newly injected private context args; legacy tools still do for compatibility.
- 2026-05-07: Added explicit test coverage for both context-aware arg cleanliness and legacy private arg injection.
- 2026-05-07: Focused tests passed: `pytest -q eggthreads/tests/test_plugin_tool_registry.py eggthreads/tests/test_repl_dynamic_tool_wrappers.py eggthreads/tests/test_repl_bridge.py`.
- 2026-05-07: Added `ToolCapabilities` registry metadata with `supports_streaming`, `supports_cancellation`, and extra metadata.
- 2026-05-07: Exported `ToolCapabilities` from `eggthreads`.
- 2026-05-07: Marked built-in `bash` as streaming+cancellable and `python` as cancellable.
- 2026-05-07: Added tests proving capabilities are stored as registry metadata and not exposed in LLM tool schemas.
- 2026-05-07: Focused tests passed: `pytest -q eggthreads/tests/test_plugin_tool_registry.py eggthreads/tests/test_repl_dynamic_tool_wrappers.py eggthreads/tests/test_repl_bridge.py`.

## Phase 4 — Command registry and internal command plugins

Goal: replace mixin-name dispatch with a runtime command registry.

Commands should be registered by the same feature plugins that register related tools/providers/policies. Do not create command-only modules that duplicate tool behavior unless the command is purely UI/display related.

- [ ] Add `CommandRegistry`, `CommandSpec`, and `CommandResult`.
  - Suggested `CommandSpec` fields:
    - `name`
    - `aliases`
    - `category`
    - `usage`
    - `description`
    - `handler`
    - `complete`
  - Suggested `CommandResult` fields:
    - `clear_input`
    - `exit_app`
    - `switched_thread`
    - `start_schedulers`
    - `message`
- [ ] Add `CommandContext`.
  - Include stable operations instead of requiring raw app access:
    - `db`
    - `current_thread`
    - setter for current thread
    - `log_system`
    - `console_print_block`
    - scheduler helpers
    - model/client/system prompt accessors
  - Built-in UI-only commands may receive an app escape hatch if necessary.
- [ ] Make `/help` generated from command metadata.
- [ ] Make autocomplete use command registry metadata and completion callbacks.
- [ ] Add input-prefix handler registry for `$` and `$$`.
  - This decouples shell commands from hardcoded app input handling.
- [ ] Migrate commands in small groups:
  - [ ] core/lifecycle: `/help`, `/quit`, `/reload`.
  - [ ] tools admin: `/toolsOn`, `/toolsOff`, `/disableTool`, `/enableTool`, `/toolsStatus`, `/toolInfo`, `/toolsSecrets`, `/toggleAutoApproval`.
  - [ ] thread UI: `/threads`, `/thread`, `/newThread`, `/deleteThread`, `/duplicateThread`, `/parentThread`, `/listChildren`, `/continue`.
  - [ ] subagents: `/spawnChildThread`, `/spawnAutoApprovedChildThread`, `/waitForThreads`.
  - [ ] session: `/sessionStatus`, `/sessionOn`, `/sessionOff`, `/sessionStop`, `/sessionReset`, `/sessionCleanup`, `/pythonRepl`, `/bashRepl`.
  - [ ] sandbox admin: `/toggleSandboxing`, `/setSandboxConfiguration`, `/getSandboxingConfig`.
  - [ ] skills: `/skills`, `/skill`.
  - [ ] web: `/startSearxng`, `/stopSearxng`.
  - [ ] display/input TUI: `/togglePanel`, `/toggleBorders`, `/redraw`, `/displayMode`, `/paste`, `/enterMode`.
  - [ ] model/auth: `/model`, `/updateAllModels`, `/login`, `/logout`, `/authStatus`.
- [ ] During each command migration, reuse the plugin's shared service layer instead of copying logic from the old mixin.
  - Example: subagent commands should call the same spawn/wait services as `spawn_agent` and `wait` tools.
  - Example: session commands should call the same status/stop/reset target-resolution helpers as session tools.
  - Example: skills commands should call the same list/load skill service as the `skill` tool.
  - Example: web commands should share SearXNG/backend helpers with web tools.
- [ ] Remove obsolete mixin dispatch only after all commands are registered.
- [ ] Commit after each command group.

## Phase 5 — Sandbox provider plugins

Goal: make sandbox providers pluggable while keeping enforcement core.

- [ ] Introduce `SandboxProviderRegistry`.
- [ ] Define provider interface.
  - Prefer a full execution interface over only `wrap_argv()`:
    - availability/status;
    - config validation;
    - run command with cwd/env/config/context;
    - stream output;
    - cancel/cleanup.
- [ ] Move existing Docker provider behind the registry.
- [ ] Move existing SRT provider behind the registry if still supported.
- [ ] Move existing bwrap provider behind the registry if still supported.
- [ ] Preserve current sandbox config event format where possible.
- [ ] Keep fail-closed semantics available for policies that require sandboxing.
- [ ] Add tests for provider selection and unavailable-provider warnings.
- [ ] Commit per provider.

Security note:
- A plugin sandbox provider can implement the boundary, but core must ensure tools cannot accidentally bypass the selected boundary.
- In-process host-side tools are trusted. Untrusted tools eventually need out-of-process execution.

## Phase 6 — Session provider plugins

Goal: make persistent execution sessions provider-based.

- [ ] Introduce `SessionProviderRegistry`.
- [ ] Define `SessionProvider` interface.
  - Suggested methods:
    - `available()`
    - `status()`
    - `start()`
    - `attach()`
    - `eval()`
    - `stop()`
    - `reset()`
    - `cleanup()`
- [ ] Move memory session provider behind the registry.
- [ ] Move Docker session provider behind the registry.
- [ ] Keep eval-token authorization and `eggtools` bridge protocol core.
- [ ] Ensure session runtime wrapper generation uses active tool registry.
- [ ] Add room for future providers:
  - containerd;
  - Podman;
  - VM/microVM;
  - SSH/remote worker;
  - Kubernetes pod.
- [ ] Commit per provider.

## Phase 7 — Approval policy plugins

Goal: allow pluggable approval strategies without giving plugins authority to mutate TC state directly.

- [ ] Define `ApprovalPolicy` interface.
  - Input: tool call, parsed args, tool metadata, caller origin, thread policy, sandbox/session status.
  - Output: verdict only.
- [ ] Define verdict model.
  - Suggested decisions:
    - `allow`
    - `deny`
    - `require_human`
    - `abstain`
- [ ] Implement core aggregation.
  - Conservative rule suggestion:
    - deny wins;
    - require-human beats allow;
    - allow only if no policy denies/requires-human;
    - abstain means no opinion;
    - no decisive policy falls back to existing behavior.
- [ ] Move current auto-approval/manual approval defaults into built-in policies where appropriate.
- [ ] Add audit events for policy evaluations.
- [ ] Add a placeholder interface suitable for future small-LLM approver plugin.
- [ ] Do not implement LLM approval until the deterministic policy chain is tested.
- [ ] Commit.

LLM approver design notes:
- It should be an advisor, not the final authority.
- It should receive sanitized/minimal context.
- It must not override hard sandbox/tool capability constraints.
- It should be able to return `require_human` for uncertainty.

## Phase 8 — Output publication/truncation/secrets plugins

Goal: separate raw captured output, UI output, LLM-visible output, artifacts, redaction, and summaries.

- [ ] Define output channel model.
  - Suggested channels:
    - raw captured output;
    - stored artifact;
    - UI-visible preview;
    - LLM-visible message;
    - audit metadata.
- [ ] Define `OutputPolicy` interface.
  - Input: tool output, tool metadata, thread config, caller origin, current limits.
  - Output: proposed publication decision.
- [ ] Implement built-in default policy chain matching current behavior.
  - terminal safety;
  - secret masking config;
  - truncation/stashing;
  - output approval defaults;
  - `no_api` handling.
- [ ] Keep core-owned hard limits to avoid DB/UI blowups.
- [ ] Add policy composition rules.
- [ ] Add tests for:
  - long output;
  - hidden `$$` output;
  - raw vs masked mode;
  - omitted output;
  - artifact/stash preview;
  - terminal control sanitization.
- [ ] Commit after each policy-stage migration.

Future plugin examples:
- DLP scanner;
- small-model tool-output summarizer;
- raw-local-mode policy;
- enterprise audit policy;
- citation/artifact policy.

## Phase 9 — Context compaction and memory plugins

Goal: make context construction, compaction, and memory extensible without corrupting the raw event log.

Core invariant:
- The raw event transcript remains the source of truth.
- Compaction/memory plugins may add derived events, summaries, artifacts, or injected context, but must not erase authoritative history.

### 9.1 Context hook points

- [ ] Add `ContextPolicy` or hook registry for context construction.
- [ ] Add `pre_llm_call(messages, ctx) -> messages` hook.
  - First implementation can be read-only/injection-only.
- [ ] Add `on_context_pressure(ctx)` hook.
  - Trigger when token estimate approaches thread/model context limit.
- [ ] Add `post_assistant_message(message, ctx)` hook.
  - Enables memory extraction after assistant/user turns.
- [ ] Add `on_thread_idle(ctx)` hook if useful for background compaction/memory extraction.

### 9.2 Compaction plugins

- [ ] Define compaction decision model.
  - Example decisions:
    - no-op;
    - summarize range;
    - replace provider-visible context with summary plus recent tail;
    - stash omitted details as artifact;
    - require user confirmation.
- [ ] Core should own token-budget accounting and final provider-visible message assembly.
- [ ] Plugin should own summarization strategy.
  - Examples:
    - deterministic extractive summary;
    - small-LLM abstractive summary;
    - code-aware compaction;
    - tool-output-specific compaction.
- [ ] Add provenance metadata for every summary:
  - source event range;
  - model/policy used;
  - timestamp;
  - whether secrets/no_api content was included.
- [ ] Ensure `no_api` and secret-masking rules are preserved in compacted context.

### 9.3 Memory plugins

- [ ] Define `MemoryProvider` interface.
  - Suggested methods:
    - `store(memory_item, ctx)`
    - `retrieve(query, ctx)`
    - `delete(scope, selector)`
    - `status()`
- [ ] Define memory item metadata.
  - scope: global/user/project/thread/subtree;
  - provenance;
  - sensitivity/privacy flags;
  - expiry/ttl;
  - embedding/model info if applicable.
- [ ] Add built-in simple memory provider first.
  - Could be SQLite or files under `.egg/memory/`.
  - Keep it disabled unless explicitly enabled.
- [ ] Add retrieval injection through `pre_llm_call`.
- [ ] Add extraction through `post_assistant_message` or explicit command/tool.
- [ ] Add commands/tools later:
  - `/memoryStatus`
  - `/memorySearch`
  - `/memoryForget`
  - possibly `remember` / `recall` tools.
- [ ] Commit in small substeps.

Memory safety notes:
- Memory plugins must respect `no_api` and secret policy.
- Retrieval should include provenance so the model knows where context came from.
- Users need a way to inspect and delete memory.
- Default should be conservative: no persistent cross-thread memory unless enabled.

## Phase 10 — External plugin discovery and configuration

Goal: allow third-party plugins after internal plugin interfaces stabilize.

- [ ] Add plugin config model.
  - Global config location TBD.
  - Per-thread enable/disable can be a later extension.
- [ ] Add explicit allowlist of enabled external plugins.
  - Do not auto-load every installed entry point by default.
- [ ] Add Python entry-point discovery.
  - Suggested group: `egg.plugins`.
- [ ] Add plugin metadata/status command.
  - Example: `/pluginsStatus`.
- [ ] Add failure isolation.
  - Failed plugin load should be reported clearly and should not corrupt core startup.
- [ ] Add plugin capability metadata.
  - A plugin should be able to declare which extension types it contributes:
    - tools;
    - commands;
    - input handlers;
    - providers;
    - policies;
    - hooks;
    - context/memory;
    - help/autocomplete.
  - `/pluginsStatus` should show these capabilities and registered names.
- [ ] Add tests using a fake entry point/plugin.
- [ ] Commit.

## Phase 11 — Documentation and cleanup

- [ ] Update architecture docs.
- [ ] Update command help docs to explain plugin-provided commands.
- [ ] Update tool docs to explain plugin-provided tools.
- [ ] Add examples for:
  - simple tool plugin;
  - command plugin;
  - sandbox provider plugin;
  - session provider plugin;
  - output policy plugin;
  - memory provider plugin.
- [ ] Remove obsolete comments that say tools/commands are hardcoded.
- [ ] Run broader relevant test suites.
- [ ] Commit.

## Current implementation notes

- `ToolRegistry` already exists and is the first seam to preserve.
- `create_default_tools()` is currently widely used and should remain as a compatibility wrapper until all callers migrate.
- Tools and commands are often two frontends for the same feature. Built-in plugins should use shared service modules so tool handlers and command handlers do not diverge.
- The runner currently has special handling for `bash`; do not remove it until the richer tool execution interface can fully replace it.
- Slash commands are currently dispatched by `getattr(self, f"cmd_{cmd}", None)` on mixins. The command registry should replace this gradually.
- Static command help and autocomplete currently need to become registry-driven.
- Session runtime `eggtools` wrappers are generated from the active tool registry; this must stay true after plugins are introduced.

## Last-known suggested next step

Start with **Phase 0**, then **Phase 1**. Do not begin external plugin discovery until internal built-in plugin registration is stable.
