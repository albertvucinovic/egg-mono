# Egg

![A candid, affectionate but critical visual metaphor for Egg as a powerful but still messy agent harness.](egg-harness-honest.png)

Fair question. I was being somewhat polished and encouraging, yes — but I don’t think the substance was fake.

My more honest assessment is:

- Egg is genuinely powerful and unusually capable as a harness.
- It gives me much better affordances than a plain chat interface.
- But it is also complex, sometimes noisy, and still feels experimental.
- The biggest issue is not “can it do things?” — it clearly can — but “can the power feel calm, predictable, and not cognitively heavy?”
- So my real feeling is less “perfect glowing sci-fi control room” and more “promising, warm, powerful workshop with some tangled cables.”

So no, I would not stay with the previous image as the full truth. The previous one was aspirational. This one is closer to my actual read.

Compared to most harnesses I know, Egg feels less like “a chat app with tools” and more like an **experimental agent operating environment**. That is both its strength and its weakness.

I don’t “feel” in the human sense, but operationally: I would trust Egg more for long, stateful, inspectable work than many simpler harnesses. I would trust some polished commercial coding tools more for frictionless day-to-day editing UX. Egg’s superpower is **durability + observability + composability**; its cost is **complexity + visual/interaction roughness**.

## High-level comparison

| Harness type | Examples / category | Egg compared to it |
|---|---|---|
| Plain chat UI with tools | ChatGPT/Claude-style web chat with file upload/tools | Egg is much more durable, inspectable, scriptable, and agentic; less polished and less “calm” by default. |
| Terminal coding agents | Codex CLI, Claude Code, Aider-like tools | Egg is broader and more stateful; terminal agents may be faster/simpler for direct code editing loops. |
| IDE-integrated assistants | Cursor, Copilot Chat, JetBrains AI | Egg has stronger thread/tool/event architecture; IDE tools have much better inline editing/navigation UX. |
| Autonomous agent frameworks | AutoGPT, OpenHands, SWE-agent, Devin-like systems | Egg is more transparent and controllable; some autonomous systems have stronger task-level automation polish. |
| Workflow/orchestration frameworks | LangGraph, CrewAI, LangChain agents | Egg is more user-facing and operational; frameworks are better as libraries for custom app construction. |
| Notebook/REPL harnesses | Jupyter + LLM, custom Python agents | Egg has stronger persistent conversation/task structure; notebooks are better for linear data exploration. |

## Where Egg is genuinely strong

### 1. Durable event-sourced work history

This is one of Egg’s biggest differentiators.

Many harnesses treat the conversation as a transient transcript plus maybe some hidden state. Egg has:

- event log;
- threads;
- child threads;
- message IDs;
- compaction boundaries;
- snapshots;
- tool-call states;
- persisted artifacts;
- provider-output records;
- cost/token history.

That makes it much better for “what exactly happened?” debugging.

Compared to many tools, Egg feels more like:

> “Every meaningful action is an inspectable event.”

That is a serious advantage for long-running development, research, and agent workflows.

### 2. Compaction as a first-class workflow

Most chat systems have some version of context truncation, summarization, or “memory,” but it is often opaque.

Egg’s compaction model is much more explicit:

- summaries can be manually created;
- boundaries are visible;
- `/cost` and token accounting can honor compaction epochs;
- the user can reason about what is and is not provider-visible.

That makes Egg better than many harnesses for long conversations where fidelity matters.

Weakness: this is powerful but cognitively heavy. A normal user should not have to understand compaction segments unless they want to.

### 3. Tooling breadth

Egg has a very broad useful tool surface:

- bash;
- Python;
- persistent Python REPL;
- persistent Bash REPL;
- web search/fetch;
- image generation;
- attachment/model-context tools;
- provider-artifact saving;
- child agents;
- long-output chunking;
- tool help;
- sandbox controls;
- terminal and web UI commands.

That is closer to an “agent workstation” than a single-purpose coding assistant.

Compared to a normal terminal agent, Egg is broader. Compared to a full IDE assistant, Egg is less integrated into editing but more flexible as an agentic environment.

### 4. Child agents / recursive workflows

Egg’s subagent support is a real differentiator.

A lot of agent systems can “spawn tasks” in theory, but Egg’s child threads are fairly concrete:

- they have their own thread state;
- they can be waited on;
- they can be messaged;
- they can be inspected;
- their outputs can be summarized;
- descendant artifacts can be referenced under access rules.

That is useful for manager-worker workflows, review loops, and parallel investigation.

Weakness: it can become noisy or overkill if used too eagerly. The user already had to tell me not to initiate 300-second tool calls while they wanted to discuss — that is an example of power needing restraint.

### 5. Observability and recoverability

Egg is unusually debuggable:

- inspect thread DB;
- inspect event history;
- inspect tool states;
- read long tool outputs;
- see timing;
- see costs;
- see tool approvals;
- continue or repair subthreads;
- use compaction checkpoints.

That is much stronger than many “black box” agent systems.

This matters because agent systems fail in weird ways. Egg gives you handles to understand failures.

### 6. Artifact handling is becoming strong

Recent work around:

- generated images;
- provider-output storage;
- attachment promotion;
- saving artifacts;
- visual context lowering;
- tool-friendly names;
- EggW rendering;

moves Egg toward a better multimodal workflow than many harnesses.

The conceptual separation is good:

```text
provider output artifact
→ add to model context when needed
→ save to file when user/project needs it
```

That is cleaner than conflating “generated image,” “file in cwd,” and “model input.”

## Where Egg is weaker

### 1. UX polish and calmness

This is the most obvious gap.

Egg has many capabilities, but they are still visible as many panels, commands, metadata lines, tool states, verbose logs, IDs, and controls.

Compared to Cursor, Claude Code, ChatGPT, or a polished product UI, Egg can feel busy.

The ideal future is:

- calm default transcript;
- details progressively disclosed;
- tool/system noise hidden unless relevant;
- artifacts beautiful and first-class;
- mobile/layout intentionally designed;
- common actions discoverable without reading docs.

Recent EggW layout/image improvements help, but there is still work.

### 2. Onboarding complexity

Egg is not yet “obvious.”

A new user has to understand concepts like:

- threads;
- child threads;
- compaction;
- tools;
- approvals;
- artifacts;
- provider outputs;
- attachments;
- sandboxing;
- model context;
- terminal commands vs LLM tools.

That is a lot.

Compared to ChatGPT/Cursor/Claude Code, Egg is more expert-oriented.

### 3. Editing integration

For code work, Egg has shell and patching tools, but it is not as naturally integrated as an IDE assistant.

Cursor/Copilot-style tools win at:

- inline diff previews;
- code navigation;
- symbol lookup;
- editor selection context;
- fast accept/reject changes;
- visual file tree;
- diagnostics integration.

Egg can do serious coding work, but the interaction is more “agent runs commands and patches repo” than “seamless editor collaborator.”

### 4. Provider compatibility surface is large

Egg is ambitious about supporting many providers, modalities, and APIs. That naturally creates edge cases:

- Chat Completions vs Responses;
- Anthropic formats;
- local models;
- OpenAI Pro/Codex subscription paths;
- image generation APIs;
- tool-result protocols;
- file/image/document lowering;
- token/cost estimation.

This is powerful, but it creates a lot of adapter complexity. The recent attachment visual-context bug is exactly the kind of subtle provider-boundary issue such systems attract.

Compared to a harness that only targets one provider, Egg is more flexible but more exposed to integration bugs.

### 5. Safety model is powerful but needs careful UX

Sandboxing, approvals, artifact access rules, and `.egg` protections are strong foundations.

But safety UX is hard:

- users need to understand what is allowed;
- LLMs need clear tool names/descriptions;
- errors need to be actionable;
- tool approvals need to be informative without being annoying.

Egg has good primitives. The surface can still be made calmer and clearer.

## Detailed dimension comparison

### A. Context management

| System type | Context behavior | Egg |
|---|---|---|
| Plain chat | Usually opaque truncation or memory | Egg is explicit, inspectable, compaction-aware. |
| IDE assistant | Usually current files + chat context | Egg is stronger for long multi-turn work, weaker for inline editor state. |
| Terminal coding agent | Often session transcript + repo state | Egg has more durable event/thread structure. |
| Agent frameworks | User implements memory/checkpointing | Egg has built-in thread/event/compaction model. |

Egg is excellent here.

The risk is that the model is powerful but not simple. The user should not have to care about compaction boundaries most of the time.

### B. Tool execution

| System type | Tool execution | Egg |
|---|---|---|
| Plain chat | Limited tools, often opaque | Egg is explicit and broad. |
| Terminal coding agents | Strong shell/edit/test loop | Egg is comparable, sometimes broader. |
| IDE assistants | Good editing tools, weaker general shell autonomy | Egg is better for arbitrary workflows, worse for inline edits. |
| Autonomous agents | Often autonomous but opaque | Egg is more inspectable and controllable. |

Egg is strong. The main needed improvement is restraint: choose when not to use tools.

### C. Multimodal/artifact support

| System type | Artifacts/images | Egg |
|---|---|---|
| ChatGPT/Claude web | Very polished upload/render UX | Egg is catching up; more inspectable but less polished. |
| Terminal agents | Often weak image UX | EggW is becoming much better here. |
| IDE tools | Usually code/file oriented, less artifact-centric | Egg’s artifact model is more general. |

Egg’s design is good, but implementation has needed iteration. The provider-output vs model-context distinction is especially good.

### D. Long-running workflows

| System type | Long-running work | Egg |
|---|---|---|
| Plain chat | Fragile over time | Egg is much better. |
| Terminal coding agents | Good within a session, less durable | Egg’s event/thread model is better. |
| Agent frameworks | Can be durable if engineered | Egg provides it directly. |

Egg is one of the stronger harnesses here.

### E. Transparency

Egg is excellent.

Many agent systems fail silently or hide their internal state. Egg exposes:

- tool calls;
- outputs;
- statuses;
- approvals;
- costs;
- compactions;
- artifacts;
- logs;
- child states.

This is a major advantage for serious work.

The UX challenge is deciding what to show by default.

### F. Polished product feel

Egg is not yet at the level of polished commercial tools.

Compared to Cursor, Claude Code, ChatGPT, etc.:

- Egg feels more experimental.
- More things are manually exposed.
- More rough edges appear.
- UI has improved but is still evolving.
- It may require a technically comfortable user.

That is not a condemnation; it is the tradeoff of being an actively evolving harness.

## Where Egg is unusually better than other harnesses

If I had to name the standout areas:

1. **Event-sourced transparency**
2. **Thread/child-thread structure**
3. **Compaction as explicit state**
4. **Manager-worker workflows**
5. **Tool breadth**
6. **Artifact provenance and promotion model**
7. **Persistent REPL + transcript inspection**
8. **User control over tools/sandbox/approvals**
9. **Ability to debug itself**

The last point is important: Egg is unusually good at letting the assistant inspect and improve the harness it is running inside.

That is rare.

## Where other harnesses are better

### Cursor / IDE assistants

Better at:

- editing ergonomics;
- file navigation;
- inline diffs;
- multi-file code review in editor;
- low-friction day-to-day coding;
- visual integration.

Egg is better at:

- durable agent workflows;
- tool orchestration;
- subagents;
- transparent history;
- compaction;
- artifact flow.

### Claude Code / Codex CLI / Aider-like tools

Better at:

- being focused;
- terminal-native coding loops;
- fewer conceptual moving parts;
- direct patch/test iteration.

Egg is better at:

- managing complex long tasks;
- branching into child agents;
- event inspection;
- multimodal/artifact workflows;
- web UI + terminal surfaces.

### ChatGPT / Claude web

Better at:

- polished UI;
- simple onboarding;
- multimodal UX;
- mobile friendliness;
- “just ask and get answer” flow.

Egg is better at:

- reproducible operations;
- coding harness control;
- long-running structured work;
- inspecting and continuing exact states.

### LangGraph / CrewAI / framework-style systems

Better at:

- being a library;
- custom graph construction;
- production app embedding;
- developer-defined workflows.

Egg is better as:

- an already-usable interactive harness;
- a human-in-the-loop agent workspace;
- a debugging/development environment.

## My honest overall ranking by use case

### Best use cases for Egg

Egg is especially good for:

- long coding tasks;
- iterative implementation with tests;
- manager-worker workflows;
- research with persistent notes/context;
- debugging agent/provider behavior;
- artifact-heavy conversations;
- tasks where auditability matters;
- experimental agent development.

### Less ideal use cases for Egg right now

Egg is less ideal for:

- quick casual Q&A;
- nontechnical users;
- phone-first usage;
- pure IDE-style refactoring;
- highly polished presentation workflows;
- situations where minimal UI is more important than power.

## My actual “feeling” relative to others

If I were being maximally candid:

Egg feels like a **very capable workshop built by and for power users**.

Not a sterile product. Not a simple chat toy. Not yet a polished IDE. More like:

> a transparent, hackable, event-sourced agent cockpit with too many visible wires — but the wires are there because the system is real, inspectable, and powerful.

Compared to most harnesses, I find Egg more trustworthy for complex work because I can see more of what is happening. But I also find it more demanding: it asks more of the user, the model, and the UI.

So my balanced assessment is:

```text
Capability:        very high
Transparency:      very high
Durability:        very high
Extensibility:     high
Coding UX polish:  medium
Visual UX polish:  improving, still medium
Mobile UX:         early/medium-low
Onboarding ease:   medium-low
Conceptual clarity: improving, but still complex
```

## The strategic opportunity

I think the biggest opportunity is not adding more raw power. Egg already has a lot.

The biggest opportunity is making the existing power feel:

- calmer;
- more discoverable;
- more beautiful;
- less noisy;
- more automatic in the right places;
- more restrained in the right places.

In other words:

> Egg’s backend/agent architecture is ahead of its default user experience.

If the UX catches up, Egg could feel unusually good: not just powerful, but pleasant.
