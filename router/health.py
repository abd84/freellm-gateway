"""
Per-provider health tracker.

Two data sources:
  1. Passive — every ask() call records latency + success/fail automatically
  2. Active  — background task pings each provider every PING_INTERVAL seconds
               with a minimal prompt to detect outages between user requests

Status logic (based on last 10 requests):
  up        — < 20% errors
  degraded  — 20–60% errors
  down      — > 60% errors  OR  last active ping failed
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque

PING_INTERVAL = 300   # seconds between active pings (5 min)
WINDOW = 10           # rolling window size for passive tracking
PING_PROMPT = "Reply with the single word: pong"


@dataclass
class ProviderStats:
    name: str
    # rolling window of (latency_ms, success) tuples — chat/text requests only.
    # Image generation/editing is inherently much slower (20-95s+, by nature of
    # the task, not a fault) and used to share this same window, which let a
    # handful of real image requests drag the reported "average latency" up to
    # 70-100s+ and make a perfectly healthy chat backend look broken. Tracked
    # separately now (_image_window) so each number means what it says.
    _window: Deque[tuple[float, bool]] = field(default_factory=lambda: deque(maxlen=10))
    _image_window: Deque[tuple[float, bool]] = field(default_factory=lambda: deque(maxlen=10))
    last_ping_at: float = 0.0
    last_ping_ok: bool | None = None
    last_ping_latency_ms: float | None = None
    last_error: str = ""
    total_requests: int = 0
    total_errors: int = 0

    def record(self, latency_ms: float, success: bool, error: str = "", kind: str = "chat") -> None:
        target = self._image_window if kind == "image" else self._window
        target.append((latency_ms, success))
        self.total_requests += 1
        if not success:
            self.total_errors += 1
            self.last_error = error

    @property
    def _combined(self) -> list[tuple[float, bool]]:
        """Chat + image requests together — used for status/error-rate, which
        should reflect all traffic, just not let image latency skew the
        latency-specific numbers."""
        return list(self._window) + list(self._image_window)

    @property
    def status(self) -> str:
        if self.last_ping_ok is False:
            return "down"
        combined = self._combined
        if not combined:
            return "up"
        error_rate = sum(1 for _, ok in combined if not ok) / len(combined)
        return "down" if error_rate > 0.4 else "up"

    @property
    def avg_latency_ms(self) -> float | None:
        """Chat-only average latency."""
        successes = [ms for ms, ok in self._window if ok]
        return round(sum(successes) / len(successes), 1) if successes else None

    @property
    def p90_latency_ms(self) -> float | None:
        """Chat-only p90 latency."""
        successes = sorted(ms for ms, ok in self._window if ok)
        if not successes:
            return None
        idx = int(len(successes) * 0.9)
        return round(successes[min(idx, len(successes) - 1)], 1)

    @property
    def avg_latency_ms_image(self) -> float | None:
        """Image generation/editing average latency — tracked separately since
        it's inherently much slower than chat, not comparable to it."""
        successes = [ms for ms, ok in self._image_window if ok]
        return round(sum(successes) / len(successes), 1) if successes else None

    @property
    def p90_latency_ms_image(self) -> float | None:
        successes = sorted(ms for ms, ok in self._image_window if ok)
        if not successes:
            return None
        idx = int(len(successes) * 0.9)
        return round(successes[min(idx, len(successes) - 1)], 1)

    @property
    def error_rate_pct(self) -> float:
        combined = self._combined
        if not combined:
            return 0.0
        return round(sum(1 for _, ok in combined if not ok) / len(combined) * 100, 1)

    @property
    def uptime_pct(self) -> float:
        if self.total_requests == 0:
            return 100.0
        return round((1 - self.total_errors / self.total_requests) * 100, 1)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "avg_latency_ms": self.avg_latency_ms,
            "avg_latency_ms_image": self.avg_latency_ms_image,
            "p90_latency_ms": self.p90_latency_ms,
            "p90_latency_ms_image": self.p90_latency_ms_image,
            "error_rate_pct": self.error_rate_pct,
            "uptime_pct": self.uptime_pct,
            "total_requests": self.total_requests,
            "total_errors": self.total_errors,
            "last_error": self.last_error or None,
            "last_ping_at": self.last_ping_at or None,
            "last_ping_ok": self.last_ping_ok,
            "last_ping_latency_ms": self.last_ping_latency_ms,
        }


# Global registry
_providers: dict[str, ProviderStats] = {
    name: ProviderStats(name=name)
    for name in ("claude", "gemini", "chatgpt", "kimi")
}

_ping_task: asyncio.Task | None = None


def record(backend: str, latency_ms: float, success: bool, error: str = "", kind: str = "chat") -> None:
    if backend in _providers:
        _providers[backend].record(latency_ms, success, error, kind=kind)


def get_all() -> dict[str, dict]:
    return {name: p.to_dict() for name, p in _providers.items()}


def get_one(backend: str) -> dict | None:
    p = _providers.get(backend)
    return p.to_dict() if p else None


async def _ping_provider(backend: str, model: str) -> None:
    from router.client import ask
    p = _providers[backend]
    t = time.time()
    try:
        await ask(PING_PROMPT, model, _track=False)  # don't pollute request stats
        latency = (time.time() - t) * 1000
        p.last_ping_ok = True
        p.last_ping_latency_ms = round(latency, 1)
        print(f"Health ping [{backend}] ok ({latency:.0f}ms)")
    except Exception as e:
        p.last_ping_ok = False
        p.last_ping_latency_ms = None
        p.last_error = str(e)[:120]
        print(f"Health ping [{backend}] FAILED: {e}")
    finally:
        p.last_ping_at = time.time()


# Default ping models per backend
_PING_MODELS = {
    "claude":  "claude-sonnet-4-6",
    "gemini":  "gemini-3-flash",
    "chatgpt": "gpt-4o-mini",
    "kimi":    "k2d6-chat",
}


async def _ping_loop() -> None:
    """Ping all providers in parallel every PING_INTERVAL seconds."""
    while True:
        await asyncio.sleep(PING_INTERVAL)
        await asyncio.gather(
            *[_ping_provider(b, m) for b, m in _PING_MODELS.items()],
            return_exceptions=True,
        )


async def run_initial_ping() -> None:
    """Ping all providers once at startup (non-blocking — runs in background)."""
    async def _all():
        await asyncio.gather(
            *[_ping_provider(b, m) for b, m in _PING_MODELS.items()],
            return_exceptions=True,
        )
    asyncio.create_task(_all())


def start_background_pings() -> None:
    global _ping_task
    _ping_task = asyncio.create_task(_ping_loop())


def stop_background_pings() -> None:
    if _ping_task:
        _ping_task.cancel()
