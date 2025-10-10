SCHEMA_SQL = r"""
-- Pragmas for reliability and concurrency
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS threads(
  thread_id TEXT PRIMARY KEY,          -- ULID
  name TEXT,                           -- name if set manually
  short_recap TEXT DEFAULT 'Untitled',
  status TEXT DEFAULT 'active',
  snapshot_json TEXT,
  snapshot_last_event_seq INTEGER NOT NULL DEFAULT -1,
  initial_model_key TEXT,
  depth INTEGER DEFAULT 0,             -- If a thread is a child, depth increases
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS children(
  parent_id TEXT NOT NULL,             -- ULID
  child_id  TEXT NOT NULL,             -- ULID
  waiting_until TEXT,                  -- ISO-8601 or NULL
  PRIMARY KEY(parent_id, child_id),
  FOREIGN KEY(parent_id) REFERENCES threads(thread_id) ON DELETE CASCADE,
  FOREIGN KEY(child_id)  REFERENCES threads(thread_id) ON DELETE CASCADE,
  CHECK(parent_id <> child_id)
);
CREATE INDEX IF NOT EXISTS children_waiting_idx ON children(waiting_until);
CREATE INDEX IF NOT EXISTS children_parent_wait_idx ON children(parent_id, waiting_until);
CREATE INDEX IF NOT EXISTS children_parent ON children(parent_id);

CREATE TABLE IF NOT EXISTS events(
  event_seq INTEGER PRIMARY KEY AUTOINCREMENT,  -- canonical order
  event_id  TEXT NOT NULL UNIQUE,               -- ULID, idempotency key
  ts        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  thread_id TEXT NOT NULL,                      -- ULID
  type      TEXT NOT NULL,
  msg_id    TEXT,                               -- ULID
  invoke_id TEXT,                               -- ULID
  chunk_seq INTEGER,                            -- for stream.delta
  payload_json TEXT NOT NULL,                   -- event body (JSON)
  FOREIGN KEY(thread_id) REFERENCES threads(thread_id) ON DELETE CASCADE,
  CHECK((type <> 'stream.delta') OR (invoke_id IS NOT NULL AND chunk_seq IS NOT NULL)),
  CHECK((type <> 'stream.open')  OR (invoke_id IS NOT NULL AND msg_id IS NOT NULL)),
  CHECK((type <> 'msg.edit')     OR (msg_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS events_thread_seq ON events(thread_id, event_seq);
CREATE INDEX IF NOT EXISTS events_msg_seq    ON events(msg_id, event_seq);
CREATE INDEX IF NOT EXISTS events_invoke_seq ON events(invoke_id, event_seq);
-- Dedupe stream deltas by (invoke_id, chunk_seq)
CREATE UNIQUE INDEX IF NOT EXISTS events_delta_unique ON events(invoke_id, chunk_seq) WHERE type='stream.delta';
-- Optional: fast timestamp scans
CREATE INDEX IF NOT EXISTS events_ts_idx ON events(ts);

CREATE TABLE IF NOT EXISTS open_streams(
  thread_id   TEXT PRIMARY KEY,                    -- ULID
  invoke_id   TEXT NOT NULL UNIQUE,                -- ULID
  last_chunk_seq INTEGER NOT NULL DEFAULT -1,
  owner       TEXT,
  purpose     TEXT,                                -- assistant_stream, tool, user_stream
  lease_until TEXT NOT NULL,
  heartbeat_at TEXT,                               -- lease_until source of truth, this is diagnostics
  opened_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  FOREIGN KEY(thread_id) REFERENCES threads(thread_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS open_streams_lease_idx ON open_streams(lease_until);
"""
