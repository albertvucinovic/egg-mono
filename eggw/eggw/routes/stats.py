"""Token statistics API routes for eggw backend."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from eggthreads import live_llm_tps_for_invoke, total_token_stats

from ..models import ThreadTokenStats
from .. import core

router = APIRouter(prefix="/api/threads", tags=["stats"])


@router.get("/{thread_id}/stats", response_model=ThreadTokenStats)
async def get_token_stats(thread_id: str):
    """Get token statistics for a thread."""
    if not core.db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    t = core.db.get_thread(thread_id)
    if not t:
        raise HTTPException(status_code=404, detail="Thread not found")

    # Get stats with cost estimates if llm_client is available
    stats = total_token_stats(core.db, thread_id, llm=core.llm_client)

    # Extract api_usage - fields are at top level of api_usage dict
    api_usage = stats.get("api_usage", {})
    cost_info = api_usage.get("cost_usd", {}) if isinstance(api_usage.get("cost_usd"), dict) else {}

    input_tokens = api_usage.get("total_input_tokens", 0) or 0
    output_tokens = api_usage.get("total_output_tokens", 0) or 0
    reasoning_tokens = api_usage.get("total_reasoning_tokens", 0) or 0  # Subset of output
    cached_tokens = api_usage.get("cached_input_tokens", 0) or 0  # Total cached across all calls

    streaming_tps = None
    try:
        row_open = core.db.current_open(thread_id)
        now_iso = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        if (
            row_open is not None
            and row_open["purpose"] == "llm"
            and isinstance(row_open["lease_until"], str)
            and row_open["lease_until"] > now_iso
        ):
            streaming_tps = live_llm_tps_for_invoke(core.db, str(row_open["invoke_id"]))
    except Exception:
        streaming_tps = None

    return ThreadTokenStats(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        cached_tokens=cached_tokens,
        total_tokens=input_tokens + output_tokens,  # reasoning is part of output
        cost_usd=cost_info.get("total") if cost_info else None,
        context_tokens=stats.get("context_tokens", 0) or 0,
        streaming_tps=streaming_tps,
    )
