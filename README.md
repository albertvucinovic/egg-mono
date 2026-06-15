# Egg

Egg is Entropy Gradient's local-first AI workbench.

It is built around **durable, inspectable thread trees**. A conversation is a
thread. The assistant can spawn child threads as subagents. Those subagents can
spawn more subagents. You can inspect, continue, compact, or recover any node in
the tree.

```text
main thread
├── research child
│   └── deeper child
├── implementation child
└── review child
```

This makes Egg useful for AI work that should be delegated and audited, not
hidden inside one opaque chat response.

## What it does

- terminal UI and web UI over the same local state;
- SQLite-backed threads in `.egg/threads.sqlite`;
- recursive, inspectable subagents;
- bash/Python tools with approval, streaming, timeouts, and sandboxing;
- persistent Python/Bash REPL sessions;
- compaction without deleting history;
- multi-provider LLM routing and usage/cost reporting.

## Run

```bash
git clone https://github.com/albertvucinovic/egg-mono.git
cd egg-mono
./egg/egg.sh
```

Web UI:

```bash
./eggw/eggw.sh
```

Optional:

```bash
cp dot.env.example .env
$EDITOR .env
```

## Useful commands

```text
/help                         show commands
/threads                      list threads
/thread <selector>            switch thread
/listChildren                 inspect child threads
/spawnChildThread <task>      create subagent
/waitForThreads <threads>     wait for children
/continue [msg_id=<id>]       continue a thread
/compactWithSummary           compact without losing history
/sessionOn provider=docker    enable persistent REPL sessions
/pythonRepl <code>            run persistent Python
/cost                         show usage/cost
/theme [name]                 terminal theme
```

## Packages

| Package | Purpose |
| --- | --- |
| `egg` | Terminal UI |
| `eggw` | Web UI |
| `eggthreads` | Threads, tools, scheduler, subagents, compaction |
| `eggllm` | LLM routing |
| `eggdisplay` | Terminal display library |
| `eggflow` | Async task/caching framework |
| `eggconfig` | Shared model config |

## Development

```bash
make install-dev
make test
```

## License

[MIT](LICENSE)
