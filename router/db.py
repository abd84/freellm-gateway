"""
SQLite persistence for user accounts and request logging.

Thread-safe: write lock around mutations; reads use check_same_thread=False.
DB file: router/router.db (next to this module).
"""
from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
import uuid
from pathlib import Path

_DB_PATH = Path(__file__).parent / "router.db"
_write_lock = threading.Lock()
_local = threading.local()


def _conn() -> sqlite3.Connection:
    """One connection per thread, reused."""
    if not hasattr(_local, "conn"):
        _local.conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
    return _local.conn


def _hash_key(key: str) -> str:
    """SHA-256 hash of an API key."""
    return hashlib.sha256(key.encode()).hexdigest()


def init_db() -> None:
    with _write_lock:
        c = _conn()
        c.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                email       TEXT NOT NULL DEFAULT '',
                api_key     TEXT NOT NULL UNIQUE,
                created_at  REAL NOT NULL,
                is_active   INTEGER NOT NULL DEFAULT 1,
                rate_limit_rpm INTEGER NOT NULL DEFAULT 60
            );
            CREATE INDEX IF NOT EXISTS idx_users_api_key ON users(api_key);

            CREATE TABLE IF NOT EXISTS requests (
                id                TEXT PRIMARY KEY,
                user_id           TEXT,
                model             TEXT NOT NULL,
                provider          TEXT NOT NULL,
                prompt_tokens     INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                latency_ms        REAL NOT NULL DEFAULT 0,
                success           INTEGER NOT NULL DEFAULT 1,
                error             TEXT NOT NULL DEFAULT '',
                timestamp         REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_req_user ON requests(user_id);
            CREATE INDEX IF NOT EXISTS idx_req_ts   ON requests(timestamp);
            CREATE INDEX IF NOT EXISTS idx_req_model ON requests(model);
        """)
        # Safe migration: add is_image_gen column if it doesn't exist
        req_cols = {row[1] for row in c.execute("PRAGMA table_info(requests)").fetchall()}
        if "is_image_gen" not in req_cols:
            c.execute("ALTER TABLE requests ADD COLUMN is_image_gen INTEGER NOT NULL DEFAULT 0")
            c.commit()

        # Safe migration: add api_key_hash column and backfill from plaintext keys
        user_cols = {row[1] for row in c.execute("PRAGMA table_info(users)").fetchall()}
        if "api_key_hash" not in user_cols:
            c.execute("ALTER TABLE users ADD COLUMN api_key_hash TEXT NOT NULL DEFAULT ''")
            c.execute("CREATE INDEX IF NOT EXISTS idx_users_api_key_hash ON users(api_key_hash)")
            c.commit()
        # Backfill hashes for existing rows that have a plaintext key but no hash
        rows = c.execute("SELECT id, api_key FROM users WHERE api_key_hash = '' AND api_key != ''").fetchall()
        for row in rows:
            c.execute("UPDATE users SET api_key_hash = ? WHERE id = ?", (_hash_key(row["api_key"]), row["id"]))
        if rows:
            c.commit()


# ── Users ────────────────────────────────────────────────────────────────────

DEFAULT_RATE_LIMIT_RPM = 60  # 0 means unlimited — never the default; set explicitly per-user if needed


def create_user(name: str, email: str = "", rate_limit_rpm: int = DEFAULT_RATE_LIMIT_RPM) -> dict:
    api_key = str(uuid.uuid4())
    row = {
        "id": str(uuid.uuid4()),
        "name": name,
        "email": email,
        "api_key": api_key,
        "api_key_hash": _hash_key(api_key),
        "created_at": time.time(),
        "is_active": 1,
        "rate_limit_rpm": rate_limit_rpm,
    }
    with _write_lock:
        _conn().execute(
            "INSERT INTO users (id, name, email, api_key, api_key_hash, created_at, is_active, rate_limit_rpm) "
            "VALUES (:id, :name, :email, :api_key, :api_key_hash, :created_at, :is_active, :rate_limit_rpm)",
            row,
        )
        _conn().commit()
    # Return full key once — caller must save it, it's not stored
    return {**row, "api_key": api_key}


def get_user_by_key(api_key: str) -> dict | None:
    key_hash = _hash_key(api_key)
    # Try hash lookup first (new rows), then plaintext fallback (legacy rows)
    row = _conn().execute(
        "SELECT * FROM users WHERE api_key_hash = ? AND is_active = 1", (key_hash,)
    ).fetchone()
    if not row:
        row = _conn().execute(
            "SELECT * FROM users WHERE api_key = ? AND is_active = 1", (api_key,)
        ).fetchone()
    return dict(row) if row else None


def list_users() -> list[dict]:
    return [dict(r) for r in _conn().execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()]


def deactivate_user(user_id: str) -> None:
    with _write_lock:
        _conn().execute("UPDATE users SET is_active = 0 WHERE id = ?", (user_id,))
        _conn().commit()


# ── Request logging ──────────────────────────────────────────────────────────

def log_request(user_id: str | None, model: str, provider: str,
                prompt_tokens: int, completion_tokens: int,
                latency_ms: float, success: bool, error: str = "",
                is_image_gen: bool = False) -> None:
    row = {
        "id": str(uuid.uuid4()),
        "user_id": user_id or "",
        "model": model,
        "provider": provider,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "latency_ms": round(latency_ms, 1),
        "success": 1 if success else 0,
        "error": error,
        "timestamp": time.time(),
        "is_image_gen": 1 if is_image_gen else 0,
    }
    with _write_lock:
        _conn().execute(
            "INSERT INTO requests (id, user_id, model, provider, prompt_tokens, completion_tokens, "
            "latency_ms, success, error, timestamp, is_image_gen) "
            "VALUES (:id, :user_id, :model, :provider, :prompt_tokens, :completion_tokens, "
            ":latency_ms, :success, :error, :timestamp, :is_image_gen)",
            row,
        )
        _conn().commit()


# ── Stats queries ────────────────────────────────────────────────────────────

def get_user_stats(user_id: str) -> dict:
    c = _conn()
    row = c.execute("""
        SELECT COUNT(*) as total, COALESCE(SUM(prompt_tokens + completion_tokens), 0) as tokens,
               SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as errors,
               AVG(CASE WHEN success = 1 THEN latency_ms END) as avg_latency,
               MAX(timestamp) as last_used
        FROM requests WHERE user_id = ?
    """, (user_id,)).fetchone()
    return {
        "total_requests": row["total"],
        "total_tokens": row["tokens"],
        "total_errors": row["errors"] or 0,
        "avg_latency_ms": round(row["avg_latency"], 1) if row["avg_latency"] else None,
        "last_used": row["last_used"],
    }


def get_global_db_stats() -> dict:
    c = _conn()
    row = c.execute("""
        SELECT COUNT(*) as total, COALESCE(SUM(prompt_tokens + completion_tokens), 0) as tokens,
               SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as errors,
               AVG(CASE WHEN success = 1 THEN latency_ms END) as avg_latency,
               MAX(timestamp) as last_used
        FROM requests
    """).fetchone()
    return {
        "total_requests": row["total"],
        "total_tokens": row["tokens"],
        "total_errors": row["errors"] or 0,
        "avg_latency_ms": round(row["avg_latency"], 1) if row["avg_latency"] else None,
        "last_used": row["last_used"],
    }


def get_recent_requests_db(limit: int = 50, user_id: str = "",
                           model: str = "", provider: str = "") -> list[dict]:
    sql = "SELECT * FROM requests WHERE 1=1"
    params: list = []
    if user_id:
        sql += " AND user_id = ?"
        params.append(user_id)
    if model:
        sql += " AND model = ?"
        params.append(model)
    if provider:
        sql += " AND provider = ?"
        params.append(provider)
    sql += " ORDER BY timestamp DESC LIMIT ?"
    params.append(min(limit, 500))
    return [dict(r) for r in _conn().execute(sql, params).fetchall()]


def get_top_models(limit: int = 10) -> list[dict]:
    return [dict(r) for r in _conn().execute("""
        SELECT model, provider, COUNT(*) as request_count,
               COALESCE(SUM(prompt_tokens + completion_tokens), 0) as total_tokens,
               AVG(CASE WHEN success = 1 THEN latency_ms END) as avg_latency_ms
        FROM requests GROUP BY model ORDER BY request_count DESC LIMIT ?
    """, (limit,)).fetchall()]


def get_usage_by_user() -> list[dict]:
    return [dict(r) for r in _conn().execute("""
        SELECT u.id, u.name, COUNT(r.id) as requests,
               COALESCE(SUM(r.prompt_tokens + r.completion_tokens), 0) as tokens,
               MAX(r.timestamp) as last_used
        FROM users u LEFT JOIN requests r ON u.id = r.user_id
        WHERE u.is_active = 1
        GROUP BY u.id ORDER BY requests DESC
    """).fetchall()]


def get_user_model_breakdown(user_id: str = "") -> list[dict]:
    sql = """
        SELECT model, provider, COUNT(*) as requests,
               COALESCE(SUM(prompt_tokens + completion_tokens), 0) as tokens,
               SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as errors,
               AVG(CASE WHEN success = 1 THEN latency_ms END) as avg_latency_ms
        FROM requests
    """
    params: list = []
    if user_id:
        sql += " WHERE user_id = ?"
        params.append(user_id)
    sql += " GROUP BY model, provider ORDER BY requests DESC"
    rows = _conn().execute(sql, params).fetchall()
    return [{
        "model": r["model"], "provider": r["provider"],
        "requests": r["requests"], "tokens": r["tokens"],
        "errors": r["errors"] or 0,
        "avg_latency_ms": round(r["avg_latency_ms"], 1) if r["avg_latency_ms"] else None,
    } for r in rows]


def get_user_provider_breakdown(user_id: str = "") -> list[dict]:
    sql = """
        SELECT provider, COUNT(*) as requests,
               COALESCE(SUM(prompt_tokens + completion_tokens), 0) as tokens,
               SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as errors,
               AVG(CASE WHEN success = 1 THEN latency_ms END) as avg_latency_ms
        FROM requests
    """
    params: list = []
    if user_id:
        sql += " WHERE user_id = ?"
        params.append(user_id)
    sql += " GROUP BY provider ORDER BY requests DESC"
    rows = _conn().execute(sql, params).fetchall()
    return [{
        "provider": r["provider"], "requests": r["requests"],
        "tokens": r["tokens"], "errors": r["errors"] or 0,
        "success_rate_pct": round((1 - (r["errors"] or 0) / r["requests"]) * 100, 1) if r["requests"] else 100.0,
        "avg_latency_ms": round(r["avg_latency_ms"], 1) if r["avg_latency_ms"] else None,
    } for r in rows]


def get_global_stats_db(started_at: float) -> dict:
    """Persistent global stats from SQLite (survives restarts)."""
    c = _conn()
    row = c.execute("""
        SELECT COUNT(*) as total_requests,
               COALESCE(SUM(prompt_tokens), 0) as prompt_tokens,
               COALESCE(SUM(completion_tokens), 0) as completion_tokens,
               SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as total_errors
        FROM requests
    """).fetchone()

    lats = [r[0] for r in c.execute(
        "SELECT latency_ms FROM requests WHERE success=1 ORDER BY latency_ms"
    ).fetchall()]

    def pct(p):
        if not lats:
            return None
        idx = min(int(len(lats) * p), len(lats) - 1)
        return round(lats[idx], 1)

    prows = c.execute("""
        SELECT provider, COUNT(*) as requests,
               COALESCE(SUM(prompt_tokens + completion_tokens), 0) as tokens,
               SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as errors
        FROM requests GROUP BY provider
    """).fetchall()
    by_provider = {
        r["provider"]: {"requests": r["requests"], "tokens": r["tokens"], "errors": r["errors"]}
        for r in prows
    }

    total_req = row["total_requests"]
    total_err = row["total_errors"] or 0
    prompt_tok = row["prompt_tokens"]
    comp_tok = row["completion_tokens"]

    return {
        "uptime_seconds": round(time.time() - started_at),
        "total_requests": total_req,
        "total_errors": total_err,
        "success_rate_pct": round((1 - total_err / total_req) * 100, 1) if total_req else 100.0,
        "tokens": {
            "prompt": prompt_tok,
            "completion": comp_tok,
            "total": prompt_tok + comp_tok,
        },
        "latency_ms": {
            "avg": round(sum(lats) / len(lats), 1) if lats else None,
            "p50": pct(0.50),
            "p90": pct(0.90),
            "p99": pct(0.99),
        },
        "throughput": {"rpm": None, "rph": None},
        "by_provider": by_provider,
    }


def get_all_providers_stats_db() -> dict:
    """Persistent per-provider stats from SQLite."""
    c = _conn()
    rows = c.execute("""
        SELECT provider,
               COUNT(*) as total_requests,
               COALESCE(SUM(prompt_tokens), 0) as prompt_tokens,
               COALESCE(SUM(completion_tokens), 0) as completion_tokens,
               SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as total_errors,
               AVG(CASE WHEN success = 1 THEN latency_ms END) as avg_latency_ms
        FROM requests GROUP BY provider
    """).fetchall()
    result = {}
    for r in rows:
        total_req = r["total_requests"]
        total_err = r["total_errors"] or 0
        result[r["provider"]] = {
            "provider": r["provider"],
            "total_requests": total_req,
            "total_errors": total_err,
            "success_rate_pct": round((1 - total_err / total_req) * 100, 1) if total_req else 100.0,
            "tokens": {
                "prompt": r["prompt_tokens"],
                "completion": r["completion_tokens"],
                "total": r["prompt_tokens"] + r["completion_tokens"],
            },
            "avg_latency_ms": round(r["avg_latency_ms"], 1) if r["avg_latency_ms"] else None,
            "latency_ms": {"avg": round(r["avg_latency_ms"], 1) if r["avg_latency_ms"] else None},
        }
    return result


def get_all_models_db() -> list[dict]:
    """Persistent per-model stats from SQLite."""
    rows = _conn().execute("""
        SELECT model, provider, COUNT(*) as total_requests,
               COALESCE(SUM(prompt_tokens), 0) as prompt_tokens,
               COALESCE(SUM(completion_tokens), 0) as completion_tokens,
               SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as total_errors,
               AVG(CASE WHEN success = 1 THEN latency_ms END) as avg_latency_ms,
               MAX(timestamp) as last_used_at
        FROM requests GROUP BY model, provider ORDER BY total_requests DESC
    """).fetchall()
    return [{
        "model": r["model"],
        "provider": r["provider"],
        "total_requests": r["total_requests"],
        "total_errors": r["total_errors"] or 0,
        "tokens": {
            "prompt": r["prompt_tokens"],
            "completion": r["completion_tokens"],
            "total": r["prompt_tokens"] + r["completion_tokens"],
        },
        "avg_latency_ms": round(r["avg_latency_ms"], 1) if r["avg_latency_ms"] else None,
        "latency_ms": {"avg": round(r["avg_latency_ms"], 1) if r["avg_latency_ms"] else None},
        "last_used_at": r["last_used_at"],
    } for r in rows]


def get_image_gen_stats(user_id: str = "") -> dict:
    where = "WHERE is_image_gen = 1"
    params: list = []
    if user_id:
        where += " AND user_id = ?"
        params.append(user_id)
    c = _conn()
    total = c.execute(f"SELECT COUNT(*) as cnt FROM requests {where}", params).fetchone()["cnt"]
    rows = c.execute(f"SELECT provider, COUNT(*) as cnt FROM requests {where} GROUP BY provider", params).fetchall()
    by_provider = {r["provider"]: r["cnt"] for r in rows}
    return {"total_image_requests": total, "by_provider": by_provider}
