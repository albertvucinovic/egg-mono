"""Token statistics API routes for eggw backend."""
from __future__ import annotations

import asyncio
from datetime import datetime

from fastapi import APIRouter, HTTPException

from eggthreads import ThreadsDB, live_llm_tps_for_invoke, thread_token_stats

from ..models import ThreadTokenStats
from .. import core

router = APIRouter(prefix="/api/threads", tags=["stats"])


def _thread_token_stats_response(stats: dict, streaming_tps: float | None = None) -> ThreadTokenStats:
    # Extract api_usage - fields are at top level of api_usage dict
    api_usage = stats.get("api_usage", {})
    if not isinstance(api_usage, dict):
        api_usage = {}
    cost_info = api_usage.get("cost_usd", {}) if isinstance(api_usage.get("cost_usd"), dict) else {}

    def _int(value) -> int:
        try:
            return int(value or 0)
        except Exception:
            return 0

    input_tokens = _int(api_usage.get("total_input_tokens"))
    output_tokens = _int(api_usage.get("total_output_tokens"))
    reasoning_tokens = _int(api_usage.get("total_reasoning_tokens"))  # Subset of output
    cached_input_tokens = _int(api_usage.get("cached_input_tokens"))
    cached_tokens_last = _int(api_usage.get("cached_tokens"))
    cache_creation_input_tokens = _int(api_usage.get("cache_creation_input_tokens"))
    approx_call_count = _int(api_usage.get("approx_call_count"))
    actual_call_count = _int(api_usage.get("actual_call_count"))
    estimated_call_count = api_usage.get("estimated_call_count")
    if estimated_call_count is None:
        estimated_call_count = max(approx_call_count - actual_call_count, 0)
    else:
        estimated_call_count = _int(estimated_call_count)
    cached_input_hit_rate = (float(cached_input_tokens) / float(input_tokens) * 100.0) if input_tokens > 0 else 0.0
    context_tokens = _int(stats.get("context_tokens"))
    full_thread_tokens = _int(stats.get("full_thread_tokens", context_tokens))
    compacted_away_tokens = max(0, full_thread_tokens - context_tokens)
    cost_total = cost_info.get("total") if cost_info else None
    cost_warnings = cost_info.get("warnings") if isinstance(cost_info.get("warnings"), list) else []

    return ThreadTokenStats(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        cached_tokens=cached_input_tokens,
        total_tokens=input_tokens + output_tokens,  # reasoning is part of output
        cost_usd=cost_total,
        context_tokens=context_tokens,
        full_thread_tokens=full_thread_tokens,
        streaming_tps=streaming_tps,
        current_provider_context_tokens=context_tokens,
        full_thread_context_tokens=full_thread_tokens,
        compacted_away_tokens=compacted_away_tokens,
        cached_input_tokens=cached_input_tokens,
        cached_tokens_last=cached_tokens_last,
        cached_input_hit_rate=cached_input_hit_rate,
        cache_creation_input_tokens=cache_creation_input_tokens,
        cache_creation_5m_input_tokens=_int(api_usage.get("cache_creation_5m_input_tokens")),
        cache_creation_1h_input_tokens=_int(api_usage.get("cache_creation_1h_input_tokens")),
        approx_call_count=approx_call_count,
        actual_call_count=actual_call_count,
        estimated_call_count=estimated_call_count,
        cost_total_usd=cost_total,
        cost_usd_details=cost_info or None,
        cost_warnings=cost_warnings,
        api_confirmed_usage=api_usage.get("api_confirmed_usage") if isinstance(api_usage.get("api_confirmed_usage"), dict) else None,
        api_usage=api_usage,
        api_usage_since_compaction=stats.get("api_usage_since_compaction") if isinstance(stats.get("api_usage_since_compaction"), dict) else None,
        by_model=api_usage.get("by_model") if isinstance(api_usage.get("by_model"), dict) else None,
    )


def _get_token_stats_sync(db_path: str, thread_id: str, llm_client) -> ThreadTokenStats | None:
    """Compute token statistics on a fresh DB connection off the event loop."""
    db = ThreadsDB(db_path)
    try:
        t = db.get_thread(thread_id)
        if not t:
            return None

        # Get stats with cost estimates if llm_client is available.  This can
        # be expensive for multi-million-token threads, so the FastAPI handler
        # runs it in a worker thread instead of blocking SSE delivery.
        stats = thread_token_stats(db, thread_id, llm=llm_client)

        streaming_tps = None
        try:
            row_open = db.current_open(thread_id)
            now_iso = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
            if (
                row_open is not None
                and row_open["purpose"] == "llm"
                and isinstance(row_open["lease_until"], str)
                and row_open["lease_until"] > now_iso
            ):
                streaming_tps = live_llm_tps_for_invoke(db, str(row_open["invoke_id"]))
        except Exception:
            streaming_tps = None

        return _thread_token_stats_response(stats, streaming_tps=streaming_tps)
    finally:
        try:
            db.conn.close()
        except Exception:
            pass


@router.get("/{thread_id}/stats", response_model=ThreadTokenStats)
async def get_token_stats(thread_id: str):
    """Get token statistics for a thread."""
    if not core.db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None,
        _get_token_stats_sync,
        core.db.path,
        thread_id,
        core.llm_client,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    return result
