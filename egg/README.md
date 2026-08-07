# egg

`egg` is the interactive terminal client for the Egg agent runtime. It combines
`eggthreads` conversations and tools with `eggdisplay` editing and rendering,
while keeping all project state under the directory from which it is launched.

Use this package when you want the terminal experience. The durable thread
model, schedulers, tools, approvals, sessions, and recovery logic live in
[`eggthreads`](../eggthreads/README.md); provider routing lives in
[`eggllm`](../eggllm/README.md).

## Features

- Streaming terminal chat in inline or full-screen display modes.
- Multiline editing, history, autocomplete, paste and file-attachment flows.
- Thread creation, navigation, delegation, waiting, continuation, and
  compaction.
- Tool approval and live tool-call presentation at multiple verbosity levels.
- Persistent Python and Bash REPL commands.
- Model, sandbox, tool-policy, token/cost, theme, and display controls.
- Restart-safe `/reload` that returns to the current thread.

## Run from the monorepo

From the project you want Egg to operate on:

```bash
cd /path/to/your/project
/path/to/egg-mono/egg/egg.sh
```

The launcher loads `.env` from `egg/` or the monorepo root, creates the
monorepo `venv/` and runs `make install` on first use, then starts Egg with the
caller's directory as its working directory. Conversation and runtime data are
stored in `./.egg/` there.

To install explicitly:

```bash
cd /path/to/egg-mono
python3 -m venv venv
source venv/bin/activate
make install
```

The installed console entry point is also named `egg`.

## Getting started

Configure a model provider as described in the [root quick start](../README.md#quick-start),
then launch Egg and run `/help`. Common commands include:

```text
/model                         show or change the active model
/threads                       list roots or one selected subtree
/newThread                     create another root thread
/spawnChildThread <task>       delegate work
/waitForThreads <threads>      wait for child threads
/show                          inspect transcript records
/compactWithSummary            shorten provider context without deleting history
/toolsStatus                   inspect tool configuration
/toggleSandboxing              change sandbox behavior
/pythonRepl <code>             run persistent Python code
/reload                        restart the client on the same thread
```

Commands come from the shared `eggthreads` plugin catalog plus terminal-only
commands for editing, attachments, image generation, theme, and rendering.

## Display and input

`EGG_DISPLAY_MODE` chooses the initial renderer:

- `full` (default): alternate-screen interface;
- `inline` or `classic`: native terminal scrollback.

Use `/displayMode`, `/displayVerbosity`, `/togglePanel`, `/toggleBorders`, and
`/syntaxHighlighting` to adjust presentation at runtime. The input editor
supports multiline text and command/model/thread completion; `$` and `$$`
prefixes run model-visible and model-hidden durable Bash tool calls.
`$$$` temporarily hands the terminal to an uncaptured foreground host command.

## Safety

Egg tools may read, write, or execute within the project. Without a configured
sandbox they can run with the launching user's host permissions. Review tool
requests, keep project `.egg/` data and credentials private, and inspect
`/toolsStatus` and `/getSandboxingConfig` before using untrusted prompts.

## Development

```bash
pip install -e "./egg[dev]"
PYTHONPATH=egg:eggthreads:eggconfig:eggdisplay:eggllm pytest -q egg/tests
```

Related documentation:

- [eggthreads runtime](../eggthreads/README.md)
- [eggdisplay terminal primitives](../eggdisplay/README.md)
- [EggW web client](../eggw/README.md)
