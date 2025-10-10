# eggthreads

Asyncio thread runner for a tree of AI chat Threads backed by SQLite.

- Stores database at .egg/threads.sqlite by default
- Integrates with eggllm for model streaming
- Implements per-thread lease using open_streams.invoke_id as the fence
- Emits events into events table with stream.open/delta/close and msg.create/edit
- Provides a SubtreeScheduler to opportunistically execute a whole subtree
