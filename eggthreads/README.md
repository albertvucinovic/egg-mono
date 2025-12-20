# eggthreads

Asyncio thread runner for a tree of AI chat Threads backed by SQLite.

- Stores database at .egg/threads.sqlite by default
- Integrates with eggllm for model streaming
- Implements per-thread lease using open_streams.invoke_id as the fence
- Emits events into events table with stream.open/delta/close and msg.create/edit
- Provides a SubtreeScheduler to opportunistically execute a whole subtree

## Run the headless subtree scheduler example

The repository includes a headless example that:

* creates a root thread and many child threads,
* runs a `SubtreeScheduler` to drive the entire subtree concurrently, and
* periodically prints per-thread progress using snapshot-derived token stats
  (including streaming deltas via `total_token_stats`).

### Command

Run from the project root:

```bash
python3 -u examples/headless_subtree_scheduler.py
```

This will create/update the SQLite DB at:

```text
.egg/threads.sqlite
```

### Optional environment variables

The example reads a few environment variables:

```bash
# Optional: system prompt text file
export SYSTEM_PROMPT_PATH=/path/to/systemPrompt

# Optional: eggllm model registry paths
export EGG_MODELS_PATH=models.json
export EGG_ALL_MODELS_PATH=all-models.json

# Optional: scheduler concurrency
export MAX_CONCURRENT=8
```

If `SYSTEM_PROMPT_PATH` is not set, the example will also look for a
`./systemPrompt` file in the current working directory.
