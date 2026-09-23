"""
Request tracker — SQLite-backed, mirrors gemini_proxy/tracker.py.

Gemini tracks:  flash_before / flash_after / flash_used  (integer credits)
Claude tracks:  util_5h_before / util_5h_after / util_5h_delta  (% as 0–100)
                util_7d_before / util_7d_after / util_7d_delta

Usage in a route:
    async with track("POST /v1/messages", model) as t:
        before = snapshot_quota()          # grab utilization before request
        r = await get().generate_content(...)
        t.prompt_chars   = len(prompt)
        t.response_chars = len(r.text)
        t.set_quota(before, snapshot_quota())   # diff computed on exit

Stats:
    GET /stats                  — recent calls
    GET /stats/summary/model    — grouped by model
    GET /stats/summary/endpoint — grouped by endpoint
    GET /stats/credits          — live utilization snapshot
    DELETE /stats               — clear history
"""
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "requests.db"


def _init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS requests (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id        TEXT,
            ts                TEXT,
            endpoint          TEXT,
            model             TEXT,
            status            TEXT,
            error             TEXT,
            prompt_chars      INTEGER DEFAULT 0,
            response_chars    INTEGER DEFAULT 0,
            duration_ms       REAL,
            util_5h_before    REAL,
            util_5h_after     REAL,
            util_5h_delta     REAL,
            util_7d_before    REAL,
            util_7d_after     REAL,
            util_7d_delta     REAL
        )
    """)
    con.commit()
    con.close()


def _write_record(rec: dict):
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        INSERT INTO requests
            (request_id, ts, endpoint, model, status, error,
             prompt_chars, response_chars, duration_ms,
             util_5h_before, util_5h_after, util_5h_delta,
             util_7d_before, util_7d_after, util_7d_delta)
        VALUES
            (:request_id, :ts, :endpoint, :model, :status, :error,
             :prompt_chars, :response_chars, :duration_ms,
             :util_5h_before, :util_5h_after, :util_5h_delta,
             :util_7d_before, :util_7d_after, :util_7d_delta)
    """, rec)
    con.commit()
    con.close()


def _pct(windows: dict, key: str) -> float | None:
    """Extract utilization as 0–100 from a windows snapshot."""
    w = windows.get(key, {})
    v = w.get("utilization")
    return round(v * 100, 2) if v is not None else None


def _delta(before: float | None, after: float | None) -> float | None:
    if before is not None and after is not None:
        return round(after - before, 2)
    return None


# ── Context manager ───────────────────────────────────────────────────────────

@dataclass
class _Ctx:
    endpoint: str
    model: str
    request_id: str  = field(default_factory=lambda: str(uuid.uuid4())[:8])
    prompt_chars: int  = 0
    response_chars: int = 0
    status: str  = "success"
    error: str | None = None
    _t0: float = field(default_factory=time.monotonic, init=False)
    _quota_before: dict = field(default_factory=dict, init=False)
    _quota_after: dict  = field(default_factory=dict, init=False)

    def set_quota(self, before: dict, after: dict):
        self._quota_before = before
        self._quota_after  = after


@asynccontextmanager
async def track(endpoint: str, model: str):
    ctx = _Ctx(endpoint=endpoint, model=model)
    try:
        yield ctx
    except Exception as e:
        ctx.status = "error"
        ctx.error  = str(e)
        raise
    finally:
        b5  = _pct(ctx._quota_before, "5h")
        a5  = _pct(ctx._quota_after,  "5h")
        b7  = _pct(ctx._quota_before, "7d")
        a7  = _pct(ctx._quota_after,  "7d")
        _write_record({
            "request_id":     ctx.request_id,
            "ts":             datetime.now(timezone.utc).isoformat(),
            "endpoint":       ctx.endpoint,
            "model":          ctx.model,
            "status":         ctx.status,
            "error":          ctx.error,
            "prompt_chars":   ctx.prompt_chars,
            "response_chars": ctx.response_chars,
            "duration_ms":    round((time.monotonic() - ctx._t0) * 1000, 1),
            "util_5h_before": b5,
            "util_5h_after":  a5,
            "util_5h_delta":  _delta(b5, a5),
            "util_7d_before": b7,
            "util_7d_after":  a7,
            "util_7d_delta":  _delta(b7, a7),
        })


# ── Query helpers ─────────────────────────────────────────────────────────────

def _query(sql: str, params: tuple = ()) -> list[dict]:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(sql, params).fetchall()
    con.close()
    return [dict(r) for r in rows]


def recent_requests(limit: int = 50) -> list[dict]:
    return _query("SELECT * FROM requests ORDER BY id DESC LIMIT ?", (limit,))


def summary_by_model() -> list[dict]:
    return _query("""
        SELECT
            model,
            COUNT(*)                                    AS total_calls,
            SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS successes,
            SUM(CASE WHEN status='error'   THEN 1 ELSE 0 END) AS errors,
            ROUND(AVG(duration_ms), 1)                  AS avg_duration_ms,
            ROUND(MAX(duration_ms), 1)                  AS max_duration_ms,
            SUM(prompt_chars)                           AS total_prompt_chars,
            SUM(response_chars)                         AS total_response_chars,
            ROUND(SUM(COALESCE(util_5h_delta, 0)), 2)  AS total_5h_utilization_pct,
            ROUND(SUM(COALESCE(util_7d_delta, 0)), 2)  AS total_7d_utilization_pct
        FROM requests
        GROUP BY model
        ORDER BY total_calls DESC
    """)


def summary_by_endpoint() -> list[dict]:
    return _query("""
        SELECT
            endpoint,
            COUNT(*)                                    AS total_calls,
            ROUND(AVG(duration_ms), 1)                  AS avg_duration_ms,
            ROUND(SUM(COALESCE(util_5h_delta, 0)), 2)  AS total_5h_utilization_pct
        FROM requests
        GROUP BY endpoint
        ORDER BY total_calls DESC
    """)


def clear_history():
    con = sqlite3.connect(DB_PATH)
    con.execute("DELETE FROM requests")
    con.commit()
    con.close()


_init_db()
