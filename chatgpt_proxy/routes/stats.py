"""
Stats endpoints.

GET  /stats                  — last 50 requests (raw rows)
GET  /stats?limit=N          — last N requests
GET  /stats/summary/model    — grouped by model: calls, avg duration
GET  /stats/summary/endpoint — grouped by endpoint
DELETE /stats                — clear history
"""
from fastapi import APIRouter, Query

from chatgpt_proxy.tracker import (
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


@router.delete("")
def delete_history():
    clear_history()
    return {"cleared": True}
