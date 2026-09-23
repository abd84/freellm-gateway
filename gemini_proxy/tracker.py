"""
Request tracker — SQLite-backed, async context manager interface.

Usage in a route:
    async with track("POST /generate/text", req.model) as t:
        r = await gemini.generate_content(req.prompt, model=req.model)
        t.prompt_chars  = len(req.prompt)
        t.response_chars = len(r.text)
    # on exit: snapshots quota diff, computes duration, writes to DB

Stats:
    GET /stats          — recent calls
    GET /stats/summary  — grouped by model/endpoint
    GET /stats/credits  — live quota snapshot
"""
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import gemini_proxy.client as _c

DB_PATH = Path(__file__).parent / "requests.db"

# Quota bucket keys → friendly names
QUOTA_BUCKETS = {
    "None-11": "flash",
    "None-4":  "pro",
}


def _init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS requests (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id      TEXT,
            ts              TEXT,           -- ISO 8601 UTC
            endpoint        TEXT,
            model           TEXT,
            status          TEXT,           -- success | error
            error           TEXT,
            prompt_chars    INTEGER DEFAULT 0,
            response_chars  INTEGER DEFAULT 0,
            image_count     INTEGER DEFAULT 0,
            duration_ms     REAL,
            flash_before    INTEGER,
            flash_after     INTEGER,
            flash_used      INTEGER,        -- before - after (positive = consumed)
            pro_before      INTEGER,
            pro_after       INTEGER,
            pro_used        INTEGER
        )
    """)
    con.commit()
    con.close()


def _snapshot_quotas() -> dict[str, int | None]:
    """Read current remaining credits from cached client.quotas."""
    q = _c.gemini.quotas if _c.gemini else {}
    snap = {}
    for key, bucket_name in QUOTA_BUCKETS.items():
        bucket = q.get(key, {})
        snap[bucket_name] = bucket.get("remaining")
    return snap


async def _refresh_quotas():
    """Force a live quota refresh from Gemini (adds ~100ms)."""
    try:
        await _c.gemini._fetch_quota()
    except Exception:
        pass  # best-effort


def _write_record(rec: dict):
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        INSERT INTO requests
            (request_id, ts, endpoint, model, status, error,
             prompt_chars, response_chars, image_count, duration_ms,
             flash_before, flash_after, flash_used,
             pro_before, pro_after, pro_used)
        VALUES
            (:request_id, :ts, :endpoint, :model, :status, :error,
             :prompt_chars, :response_chars, :image_count, :duration_ms,
             :flash_before, :flash_after, :flash_used,
             :pro_before, :pro_after, :pro_used)
    """, rec)
    con.commit()
    con.close()


# ── Context manager ───────────────────────────────────────────────────────────

@dataclass
class _Ctx:
    endpoint: str
    model: str
    request_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    prompt_chars: int = 0
    response_chars: int = 0
    image_count: int = 0
    status: str = "success"
    error: str | None = None
    _t0: float = field(default_factory=time.monotonic, init=False)
    _before: dict = field(default_factory=dict, init=False)


@asynccontextmanager
async def track(endpoint: str, model: str):
    ctx = _Ctx(endpoint=endpoint, model=model)
    ctx._before = _snapshot_quotas()

    try:
        yield ctx
    except Exception as e:
        ctx.status = "error"
        ctx.error = str(e)
        raise
    finally:
        await _refresh_quotas()
        after = _snapshot_quotas()
        duration_ms = (time.monotonic() - ctx._t0) * 1000

        def _used(bucket: str) -> int | None:
            b = ctx._before.get(bucket)
            a = after.get(bucket)
            return (b - a) if b is not None and a is not None else None

        _write_record({
            "request_id":    ctx.request_id,
            "ts":            datetime.now(timezone.utc).isoformat(),
            "endpoint":      ctx.endpoint,
            "model":         ctx.model,
            "status":        ctx.status,
            "error":         ctx.error,
            "prompt_chars":  ctx.prompt_chars,
            "response_chars": ctx.response_chars,
            "image_count":   ctx.image_count,
            "duration_ms":   round(duration_ms, 1),
            "flash_before":  ctx._before.get("flash"),
            "flash_after":   after.get("flash"),
            "flash_used":    _used("flash"),
            "pro_before":    ctx._before.get("pro"),
            "pro_after":     after.get("pro"),
            "pro_used":      _used("pro"),
        })


# ── Query helpers (used by stats routes) ─────────────────────────────────────

def _query(sql: str, params: tuple = ()) -> list[dict]:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(sql, params).fetchall()
    con.close()
    return [dict(r) for r in rows]


def recent_requests(limit: int = 50) -> list[dict]:
    return _query(
        "SELECT * FROM requests ORDER BY id DESC LIMIT ?", (limit,)
    )


def summary_by_model() -> list[dict]:
    return _query("""
        SELECT
            model,
            COUNT(*)                        AS total_calls,
            SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS successes,
            SUM(CASE WHEN status='error'   THEN 1 ELSE 0 END) AS errors,
            ROUND(AVG(duration_ms), 1)      AS avg_duration_ms,
            ROUND(MAX(duration_ms), 1)      AS max_duration_ms,
            SUM(prompt_chars)               AS total_prompt_chars,
            SUM(response_chars)             AS total_response_chars,
            SUM(image_count)                AS total_images,
            SUM(COALESCE(flash_used, 0))    AS flash_credits_used,
            SUM(COALESCE(pro_used, 0))      AS pro_credits_used
        FROM requests
        GROUP BY model
        ORDER BY total_calls DESC
    """)


def summary_by_endpoint() -> list[dict]:
    return _query("""
        SELECT
            endpoint,
            COUNT(*)                        AS total_calls,
            ROUND(AVG(duration_ms), 1)      AS avg_duration_ms,
            SUM(COALESCE(flash_used, 0))    AS flash_credits_used,
            SUM(COALESCE(pro_used, 0))      AS pro_credits_used
        FROM requests
        GROUP BY endpoint
        ORDER BY total_calls DESC
    """)


def clear_history():
    con = sqlite3.connect(DB_PATH)
    con.execute("DELETE FROM requests")
    con.commit()
    con.close()


# Init DB on import
_init_db()
