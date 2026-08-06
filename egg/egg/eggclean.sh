#!/usr/bin/env bash
set -euo pipefail

db="./.egg/threads.sqlite"
[[ -f "$db" ]] || { echo "eggclean.sh: $db not found" >&2; exit 1; }

python3 - "$db" <<'PY'
import sqlite3
import sys

connection = sqlite3.connect(sys.argv[1])
connection.execute("PRAGMA foreign_keys = ON")
with connection:
    deleted = connection.execute("""
        DELETE FROM threads
        WHERE NOT EXISTS (
              SELECT 1 FROM children
              WHERE child_id = threads.thread_id
                 OR parent_id = threads.thread_id
          )
          AND NOT EXISTS (
              SELECT 1 FROM open_streams
              WHERE open_streams.thread_id = threads.thread_id
          )
          AND 1 = (
              SELECT count(*) FROM events
              WHERE events.thread_id = threads.thread_id
                AND events.type = 'msg.create'
                AND json_extract(events.payload_json, '$.role') = 'system'
          )
          AND 1 = (
              SELECT count(*) FROM events
              WHERE events.thread_id = threads.thread_id
                AND events.type = 'msg.create'
          )
          AND NOT EXISTS (
              SELECT 1 FROM events
              WHERE events.thread_id = threads.thread_id
                AND events.type LIKE 'tool_call.%'
          )
    """).rowcount
print(f"Deleted {deleted} empty root thread(s).")
PY
