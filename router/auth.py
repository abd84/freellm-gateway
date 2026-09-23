"""
API key authentication for the AI Router.

- ADMIN_API_KEY env var -> full admin access
- User API keys from db -> per-user access
- optional_auth -> returns None for unauthenticated requests
"""
from __future__ import annotations

import hmac
import os
import re
import secrets
import time

from fastapi import HTTPException, Request, Security
from fastapi.security import APIKeyHeader

from router import db
from router.security import record_auth_failure, record_auth_success

_api_key_header = APIKeyHeader(name="Authorization", auto_error=False)
_x_api_key_header = APIKeyHeader(name="x-api-key", auto_error=False)

ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")
if not ADMIN_API_KEY:
    ADMIN_API_KEY = secrets.token_urlsafe(32)
    print("WARNING: ADMIN_API_KEY not set in .env -- generated a random key for THIS RUN ONLY:")
    print(f"  ADMIN_API_KEY={ADMIN_API_KEY}")
    print("  Set this in .env to keep it stable across restarts.")


def _keys_match(a: str, b: str) -> bool:
    """Constant-time comparison to avoid a timing side channel on the admin key."""
    return hmac.compare_digest(a, b)

# UUID-like pattern: 32-64 hex/dash chars
_KEY_PATTERN = re.compile(r"^[a-zA-Z0-9\-_]{32,64}$")


def _extract_key(raw: str | None) -> str | None:
    """Strip 'Bearer ' prefix if present."""
    if not raw:
        return None
    if raw.startswith("Bearer "):
        return raw[7:]
    return raw


def _validate_key_format(key: str) -> bool:
    """Reject obviously invalid keys before hitting the DB."""
    return bool(_KEY_PATTERN.match(key))


def _check_rate_limit(user: dict) -> None:
    """Raise 429 if user exceeds their per-minute rate limit."""
    rpm = user.get("rate_limit_rpm", 0)
    if rpm <= 0:
        return
    cutoff = time.time() - 60
    row = db._conn().execute(
        "SELECT COUNT(*) as cnt FROM requests WHERE user_id = ? AND timestamp >= ?",
        (user["id"], cutoff),
    ).fetchone()
    if row["cnt"] >= rpm:
        raise HTTPException(429, f"Rate limit exceeded ({rpm} req/min)")


async def get_current_user(request: Request, api_key: str = Security(_api_key_header), x_api_key: str = Security(_x_api_key_header)) -> dict:
    """Require a valid API key. Returns user dict with is_admin flag."""
    key = _extract_key(api_key) or _extract_key(x_api_key)
    ip = request.headers.get("X-Real-IP", request.client.host if request.client else "unknown")

    if not key:
        raise HTTPException(401, "API key required (Authorization: Bearer <key>)")

    if _keys_match(key, ADMIN_API_KEY):
        record_auth_success(ip)
        return {"id": "admin", "name": "Admin", "is_admin": True}

    # Format check before DB lookup
    if not _validate_key_format(key):
        record_auth_failure(ip)
        raise HTTPException(401, "Invalid API key")

    user = db.get_user_by_key(key)
    if not user:
        record_auth_failure(ip)
        raise HTTPException(401, "Invalid API key")

    if not user.get("is_active", 1):
        raise HTTPException(403, "Account deactivated")

    record_auth_success(ip)
    _check_rate_limit(user)
    return {**user, "is_admin": False}


async def optional_auth(request: Request, api_key: str = Security(_api_key_header), x_api_key: str = Security(_x_api_key_header)) -> dict | None:
    """Like get_current_user but returns None instead of raising on missing/invalid key."""
    key = _extract_key(api_key) or _extract_key(x_api_key)
    if not key:
        return None

    ip = request.headers.get("X-Real-IP", request.client.host if request.client else "unknown")

    if _keys_match(key, ADMIN_API_KEY):
        return {"id": "admin", "name": "Admin", "is_admin": True}

    if not _validate_key_format(key):
        return None

    user = db.get_user_by_key(key)
    if not user:
        return None

    if not user.get("is_active", 1):
        return None

    _check_rate_limit(user)
    return {**user, "is_admin": False}
