"""
Request tracker — SQLite-backed, mirrors claude_proxy/tracker.py.

ChatGPT's web interface doesn't expose quota/utilization data, so we only
track timing and character counts (no util_* columns).

Usage in a route:
    async with track("POST /v1/chat/completions", model) as t:
        t.prompt_chars   = len(prompt)
        t.response_chars = len(text)

Stats:
    GET /stats                  — recent calls
    GET /stats/summary/model    — grouped by model
    GET /stats/summary/endpoint — grouped by endpoint
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
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id      TEXT,
            ts              TEXT,
            endpoint        TEXT,
            model           TEXT,
            status          TEXT,
            error           TEXT,
            prompt_chars    INTEGER DEFAULT 0,
            response_chars  INTEGER DEFAULT 0,
            duration_ms     REAL
        )
    """)
    con.commit()
    con.close()


def _write_record(rec: dict):
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        INSERT INTO requests
            (request_id, ts, endpoint, model, status, error,
             prompt_chars, response_chars, duration_ms)
        VALUES
            (:request_id, :ts, :endpoint, :model, :status, :error,
             :prompt_chars, :response_chars, :duration_ms)
    """, rec)
    con.commit()
    con.close()


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
            SUM(response_chars)                         AS total_response_chars
        FROM requests
        GROUP BY model
        ORDER BY total_calls DESC
    """)


def summary_by_endpoint() -> list[dict]:
    return _query("""
        SELECT
            endpoint,
            COUNT(*)           AS total_calls,
            ROUND(AVG(duration_ms), 1) AS avg_duration_ms
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
