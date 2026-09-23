"""
Stats endpoints.

GET  /stats                  — last 50 requests (raw rows)
GET  /stats?limit=N          — last N requests
GET  /stats/summary/model    — grouped by model: calls, avg duration
GET  /stats/summary/endpoint — grouped by endpoint
GET  /stats/credits          — live usage % from claude.ai/api/oauth/usage (Pro/Max only)
DELETE /stats                — clear history
"""
from fastapi import APIRouter, Query

from claude_proxy.tracker import (
    clear_history,
    recent_requests,
    summary_by_endpoint,
    summary_by_model,
)

router = APIRouter(prefix="/stats")


@router.get("")
def get_requests(limit: int = Query(default=50, le=500)):
    return {"requests": recent_requests(limit)}


@router.get("/summary/model")
def get_summary_model():
    return {"summary": summary_by_model()}


@router.get("/summary/endpoint")
def get_summary_endpoint():
    return {"summary": summary_by_endpoint()}


@router.get("/credits")
def get_credits():
    """
    Live utilization snapshot from the last completed request.

    Mirrors gemini_proxy's /stats/credits but uses % instead of integer credits
    (claude.ai exposes utilization fractions, not absolute credit counts).

    Example:
        {
          "5h": { "utilization": 0.24, "remaining_pct": 76.0, "resets_at": 1788357600 },
          "7d": { "utilization": 0.03, "remaining_pct": 97.0, "resets_at": 1788372000 }
        }

    Empty dict if no requests have been made yet this session.
    """
    from claude_proxy.client import snapshot_quota
    windows = snapshot_quota()
    if not windows:
        return {"note": "No quota data yet — make at least one request first"}
    result = {}
    for key, w in windows.items():
        util = w.get("utilization", 0)
        result[key] = {
            "utilization":    round(util, 4),
            "remaining_pct":  round((1 - util) * 100, 2),
            "status":         w.get("status", "unknown"),
            "resets_at":      w.get("resets_at"),
        }
    return result


@router.delete("")
def delete_history():
    clear_history()
    return {"cleared": True}
