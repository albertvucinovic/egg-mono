import asyncio
import json
from eggthreads import ThreadsDB, total_token_stats, collect_subtree, list_active_threads, create_snapshot, word_count_from_snapshot, word_count_from_events

def _thread_token_report(db: ThreadsDB, tid: str, *, llm=None) -> tuple[int, int, float]:
    """Return (context_tokens, approx_llm_call_count, cost_usd_total)."""
    ts = total_token_stats(db, tid, llm=llm)
    try:
        ctx = int(ts.get('context_tokens') or 0)
    except Exception:
        ctx = 0
    api = ts.get('api_usage') if isinstance(ts.get('api_usage'), dict) else {}
    try:
        calls = int(api.get('approx_call_count') or 0)
    except Exception:
        calls = 0
    cost = 0.0
    try:
        cu = api.get('cost_usd') if isinstance(api.get('cost_usd'), dict) else {}
        cost = float(cu.get('total') or 0.0)
    except Exception:
        cost = 0.0
    return (ctx, calls, cost)

async def periodic_reporter(db: ThreadsDB, root_id: str, interval_sec: float = 2.0, *, llm=None) -> None:
    """Every interval_sec, print summary of active threads and token counts."""
    while True:
        try:
            await asyncio.sleep(interval_sec)
            
            subtree = collect_subtree(db, root_id)
            children = [t for t in subtree if t != root_id]
            active = list_active_threads(db, children)
            
            for tid in children:
                try:
                    th = db.get_thread(tid)
                    last_snap = int(th.snapshot_last_event_seq) if th else -1
                    row = db.conn.execute(
                        "SELECT 1 FROM events WHERE thread_id=? AND type='msg.create' AND event_seq>? LIMIT 1",
                        (tid, last_snap),
                    ).fetchone()
                    if row is not None:
                        create_snapshot(db, tid)
                except Exception as e:
                    print(f"[status] failed to snapshot {tid[-8:]}: {e}")
            
            stats = {tid: _thread_token_report(db, tid, llm=llm) for tid in children}

            active_summary = ", ".join(
                f"{tid[-8:]}({stats.get(tid, (0, 0, 0.0))[0]}t,{stats.get(tid, (0, 0, 0.0))[1]}c,${stats.get(tid, (0, 0, 0.0))[2]:.4f})"
                for tid in active
            ) or "-"
            total_ctx_tokens = sum(ctx for (ctx, _calls, _cost) in stats.values())
            total_cost = sum(cost for (_ctx, _calls, cost) in stats.values())
            print(
                f"[status] active {len(active)}/{len(children)} | "
                f"total_ctx_tokens={total_ctx_tokens} | total_cost≈${total_cost:.4f} | active: {active_summary}"
            )
        except Exception as e:
            print(f"[status] reporter error: {e}")
