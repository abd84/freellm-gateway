"""
Stats endpoints.

GET  /stats                  — last 50 requests (raw rows)
GET  /stats?limit=N          — last N requests
GET  /stats/summary/model    — grouped by model: calls, credits, avg duration
GET  /stats/summary/endpoint — grouped by endpoint
GET  /stats/credits          — live quota snapshot + totals across session
DELETE /stats                — clear history
"""
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

import gemini_proxy.client as _c
from gemini_proxy.tracker import (
    QUOTA_BUCKETS,
    _snapshot_quotas,
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
    """Live quota snapshot — calls _snapshot_quotas() from cached client data."""
    q = _c.gemini.quotas if _c.gemini else {}

    buckets = {}
    for key, name in QUOTA_BUCKETS.items():
        b = q.get(key, {})
        buckets[name] = {
            "remaining": b.get("remaining"),
            "total":     b.get("total"),
            "used":      (b["total"] - b["remaining"])
                         if b.get("total") is not None and b.get("remaining") is not None
                         else None,
            "usage_pct": round(b.get("usage_percentage", 0) * 100, 2),
            "resets_at": b.get("reset_time"),
            "label":     b.get("label"),
        }

    usage = _c.gemini.usage_info if _c.gemini else {}
    tier = usage.get("tier", {}).get("label", "unknown")

    return {"tier": tier, "quotas": buckets, "raw_usage_info": usage}


@router.delete("")
def delete_history():
    clear_history()
    return {"cleared": True}
