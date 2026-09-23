"""
Security middleware for the AI Router.

- Security headers (X-Content-Type-Options, HSTS, CSP, etc.)
- Request size limit (1MB)
- Access logging (rotating file)
- Brute force protection on auth endpoints
- CORS configuration via CORS_ORIGINS env var
"""
from __future__ import annotations

import logging
import os
import time
import threading
from collections import defaultdict
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import HTTPException, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse


# ── Access logger ────────────────────────────────────────────────────────────

_LOG_DIR = Path(__file__).parent / "logs"
_access_logger: logging.Logger | None = None


def init_access_logger() -> None:
    global _access_logger
    _LOG_DIR.mkdir(exist_ok=True)
    _access_logger = logging.getLogger("ai_router.access")
    _access_logger.setLevel(logging.INFO)
    _access_logger.propagate = False
    if not _access_logger.handlers:
        handler = RotatingFileHandler(
            _LOG_DIR / "access.log",
            maxBytes=10 * 1024 * 1024,  # 10 MB
            backupCount=5,
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        _access_logger.addHandler(handler)


# ── Brute force protection ───────────────────────────────────────────────────

_AUTH_PATHS = {"/me", "/admin"}  # prefix match

_bf_lock = threading.Lock()
_bf_failures: dict[str, list[float]] = defaultdict(list)  # ip → [timestamps]
_bf_blocked: dict[str, float] = {}  # ip → blocked_until

BF_MAX_FAILURES = 10
BF_WINDOW = 60        # seconds
BF_BLOCK_DURATION = 300  # 5 minutes


def _check_brute_force(ip: str) -> bool:
    """Return True if the IP is currently blocked."""
    now = time.time()
    with _bf_lock:
        if ip in _bf_blocked:
            if now < _bf_blocked[ip]:
                return True
            del _bf_blocked[ip]
        return False


def record_auth_failure(ip: str) -> None:
    """Record a failed auth attempt. Block IP if threshold exceeded."""
    now = time.time()
    with _bf_lock:
        entries = _bf_failures[ip]
        entries.append(now)
        # Trim old entries
        cutoff = now - BF_WINDOW
        _bf_failures[ip] = [t for t in entries if t >= cutoff]
        if len(_bf_failures[ip]) >= BF_MAX_FAILURES:
            _bf_blocked[ip] = now + BF_BLOCK_DURATION
            _bf_failures[ip] = []


def record_auth_success(ip: str) -> None:
    """Clear failure history on successful auth."""
    with _bf_lock:
        _bf_failures.pop(ip, None)


# ── Security headers middleware ──────────────────────────────────────────────

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-XSS-Protection": "1; mode=block",
    "Strict-Transport-Security": "max-age=31536000",
    "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'",
    "Referrer-Policy": "strict-origin-when-cross-origin",
}

MAX_BODY_SIZE = 20 * 1024 * 1024  # 20 MB


class SecurityMiddleware(BaseHTTPMiddleware):
    @staticmethod
    def _wrap_size_limited(request: Request) -> Request:
        """Enforce MAX_BODY_SIZE while the body streams in, for requests with
        no (or an untrustworthy) Content-Length header."""
        receive = request.receive
        total = 0

        async def limited_receive():
            nonlocal total
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body") or b"")
                if total > MAX_BODY_SIZE:
                    raise HTTPException(413, f"Request body too large. Max {MAX_BODY_SIZE} bytes.")
            return message

        request._receive = limited_receive
        return request

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        ip = request.headers.get("X-Real-IP", request.client.host if request.client else "unknown")

        # Brute force check
        is_auth_path = any(request.url.path.startswith(p) for p in _AUTH_PATHS)
        if is_auth_path and _check_brute_force(ip):
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many failed attempts. Try again later."},
            )

        # Request size limit (only for methods with bodies)
        if request.method in ("POST", "PUT", "PATCH"):
            content_length = request.headers.get("content-length")
            if content_length and int(content_length) > MAX_BODY_SIZE:
                return JSONResponse(
                    status_code=413,
                    content={"detail": f"Request body too large. Max {MAX_BODY_SIZE} bytes."},
                )
            if content_length is None:
                # No Content-Length (e.g. chunked transfer encoding) — the header
                # check above can't catch an oversized body, so enforce the cap
                # while the body streams in instead of trusting a client-supplied header.
                request = self._wrap_size_limited(request)

        t = time.time()
        response = await call_next(request)
        latency_ms = round((time.time() - t) * 1000, 1)

        # Security headers
        for header, value in _SECURITY_HEADERS.items():
            response.headers[header] = value

        # Track auth failures for brute force protection
        if is_auth_path and response.status_code == 401:
            record_auth_failure(ip)
        elif is_auth_path and response.status_code < 400:
            record_auth_success(ip)

        # Access log
        if _access_logger:
            # Hash-based hint so requests from the same key can be correlated in
            # logs without ever writing key material (even truncated) to disk.
            auth = request.headers.get("authorization", "")
            if auth:
                import hashlib as _hashlib
                user_hint = _hashlib.sha256(auth.encode()).hexdigest()[:10]
            else:
                user_hint = "-"
            _access_logger.info(
                "%s %s %s %d %.1fms user=%s",
                time.strftime("%Y-%m-%dT%H:%M:%S"),
                request.method,
                request.url.path,
                response.status_code,
                latency_ms,
                user_hint,
            )

        return response


# ── CORS setup ───────────────────────────────────────────────────────────────

def get_cors_origins() -> list[str]:
    """Parse CORS_ORIGINS env var (comma-separated). Empty = no CORS."""
    raw = os.getenv("CORS_ORIGINS", "").strip()
    if not raw:
        return []
    return [o.strip() for o in raw.split(",") if o.strip()]
