"""
Per-model and per-provider usage statistics.

Tracks per request:
  - prompt / completion / total tokens
  - latency (avg, p50, p90, p99)
  - success / error rate
  - requests per minute / hour
  - recent request history (last 500)

Token counting:
  - tiktoken for GPT models (if installed)
  - char/4 estimate for Claude, Gemini, Kimi
"""
from __future__ import annotations

import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Deque

HISTORY_SIZE = 500
LATENCY_WINDOW = 100  # rolling window for latency percentiles

_started_at: float = time.time()

# ── Token counting ────────────────────────────────────────────────────────────

def _count_tokens(text: str, model: str = "") -> int:
    if model.startswith(("gpt", "o1", "o3")):
        try:
            import tiktoken
            try:
                enc = tiktoken.encoding_for_model(model)
            except KeyError:
                enc = tiktoken.get_encoding("cl100k_base")
            return len(enc.encode(text))
        except ImportError:
            pass
    return max(1, len(text) // 4)


# ── Request record ────────────────────────────────────────────────────────────

@dataclass
class RequestRecord:
    id: str
    timestamp: float
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: float
    success: bool
    error: str | None = None
    user_id: str = ""

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "timestamp": self.timestamp,
            "provider": self.provider,
            "model": self.model,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "latency_ms": round(self.latency_ms, 1),
            "success": self.success,
            "error": self.error,
            "user_id": self.user_id,
        }


# ── Per-model stats ───────────────────────────────────────────────────────────

@dataclass
class ModelStats:
    model: str
    provider: str
    total_requests: int = 0
    total_errors: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    _latencies: Deque[float] = field(default_factory=lambda: deque(maxlen=LATENCY_WINDOW))
    _timestamps: Deque[float] = field(default_factory=lambda: deque(maxlen=3600))  # 1h of per-second granularity
    last_used_at: float | None = None
    last_error: str | None = None

    def record(self, prompt_tokens: int, completion_tokens: int,
               latency_ms: float, success: bool, error: str = "") -> None:
        self.total_requests += 1
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens
        self.last_used_at = time.time()
        self._timestamps.append(self.last_used_at)
        if success:
            self._latencies.append(latency_ms)
        else:
            self.total_errors += 1
            self.last_error = error or None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def success_rate_pct(self) -> float:
        if not self.total_requests:
            return 100.0
        return round((1 - self.total_errors / self.total_requests) * 100, 1)

    @property
    def avg_latency_ms(self) -> float | None:
        return round(sum(self._latencies) / len(self._latencies), 1) if self._latencies else None

    def _percentile(self, p: float) -> float | None:
        if not self._latencies:
            return None
        s = sorted(self._latencies)
        idx = min(int(len(s) * p), len(s) - 1)
        return round(s[idx], 1)

    @property
    def p50_latency_ms(self) -> float | None:
        return self._percentile(0.50)

    @property
    def p90_latency_ms(self) -> float | None:
        return self._percentile(0.90)

    @property
    def p99_latency_ms(self) -> float | None:
        return self._percentile(0.99)

    def _rpm(self) -> int:
        cutoff = time.time() - 60
        return sum(1 for t in self._timestamps if t >= cutoff)

    def _rph(self) -> int:
        cutoff = time.time() - 3600
        return sum(1 for t in self._timestamps if t >= cutoff)

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "provider": self.provider,
            "total_requests": self.total_requests,
            "total_errors": self.total_errors,
            "success_rate_pct": self.success_rate_pct,
            "tokens": {
                "prompt": self.prompt_tokens,
                "completion": self.completion_tokens,
                "total": self.total_tokens,
            },
            "latency_ms": {
                "avg": self.avg_latency_ms,
                "p50": self.p50_latency_ms,
                "p90": self.p90_latency_ms,
                "p99": self.p99_latency_ms,
            },
            "throughput": {
                "rpm": self._rpm(),
                "rph": self._rph(),
            },
            "last_used_at": self.last_used_at,
            "last_error": self.last_error,
        }


# ── Global registry ───────────────────────────────────────────────────────────

_models: dict[str, ModelStats] = {}
_history: deque[RequestRecord] = deque(maxlen=HISTORY_SIZE)


def _get_or_create(provider: str, model: str) -> ModelStats:
    if model not in _models:
        _models[model] = ModelStats(model=model, provider=provider)
    return _models[model]


def record(provider: str, model: str, prompt: str, response: str,
           latency_ms: float, success: bool, error: str = "",
           user_id: str = "", is_image_gen: bool = False) -> None:
    prompt_tokens = _count_tokens(prompt, model)
    completion_tokens = _count_tokens(response, model) if success else 0

    ms = _get_or_create(provider, model)
    ms.record(prompt_tokens, completion_tokens, latency_ms, success, error)

    _history.append(RequestRecord(
        id=str(uuid.uuid4())[:8],
        timestamp=time.time(),
        provider=provider,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        latency_ms=latency_ms,
        success=success,
        error=error[:120] if error else None,
        user_id=user_id,
    ))

    # Detect image generation: explicit flag or markdown image in response
    _is_image_gen = is_image_gen or (success and "![" in response)

    # Persist to SQLite
    try:
        from router import db
        db.log_request(user_id or None, model, provider,
                       prompt_tokens, completion_tokens,
                       latency_ms, success, error[:120] if error else "",
                       is_image_gen=_is_image_gen)
    except Exception:
        pass  # db not initialized yet or write failed — don't break the request


# ── Read API ──────────────────────────────────────────────────────────────────

def get_model(model: str) -> dict | None:
    ms = _models.get(model)
    return ms.to_dict() if ms else None


def get_all_models() -> list[dict]:
    return [ms.to_dict() for ms in sorted(_models.values(), key=lambda m: m.total_requests, reverse=True)]


def get_provider_stats(provider: str) -> dict:
    models = [ms for ms in _models.values() if ms.provider == provider]
    if not models:
        return {"provider": provider, "total_requests": 0, "total_errors": 0,
                "tokens": {"prompt": 0, "completion": 0, "total": 0}}

    total_req = sum(m.total_requests for m in models)
    total_err = sum(m.total_errors for m in models)
    all_latencies = [l for m in models for l in m._latencies]
    all_latencies.sort()

    def _pct(p):
        if not all_latencies:
            return None
        return round(all_latencies[min(int(len(all_latencies) * p), len(all_latencies) - 1)], 1)

    now = time.time()
    rpm = sum(sum(1 for t in m._timestamps if t >= now - 60) for m in models)
    rph = sum(sum(1 for t in m._timestamps if t >= now - 3600) for m in models)

    return {
        "provider": provider,
        "total_requests": total_req,
        "total_errors": total_err,
        "success_rate_pct": round((1 - total_err / total_req) * 100, 1) if total_req else 100.0,
        "tokens": {
            "prompt": sum(m.prompt_tokens for m in models),
            "completion": sum(m.completion_tokens for m in models),
            "total": sum(m.total_tokens for m in models),
        },
        "latency_ms": {
            "avg": round(sum(all_latencies) / len(all_latencies), 1) if all_latencies else None,
            "p50": _pct(0.50),
            "p90": _pct(0.90),
            "p99": _pct(0.99),
        },
        "throughput": {"rpm": rpm, "rph": rph},
        "models": [m.model for m in models],
    }


def get_global_stats() -> dict:
    now = time.time()
    all_ms = list(_models.values())
    total_req = sum(m.total_requests for m in all_ms)
    total_err = sum(m.total_errors for m in all_ms)
    all_latencies = sorted(l for m in all_ms for l in m._latencies)

    def _pct(p):
        if not all_latencies:
            return None
        return round(all_latencies[min(int(len(all_latencies) * p), len(all_latencies) - 1)], 1)

    rpm = sum(sum(1 for t in m._timestamps if t >= now - 60) for m in all_ms)
    rph = sum(sum(1 for t in m._timestamps if t >= now - 3600) for m in all_ms)

    providers = {}
    for provider in ("claude", "gemini", "chatgpt", "kimi", "glm"):
        ms = [m for m in all_ms if m.provider == provider]
        if ms:
            providers[provider] = {
                "requests": sum(m.total_requests for m in ms),
                "tokens": sum(m.total_tokens for m in ms),
                "errors": sum(m.total_errors for m in ms),
            }

    return {
        "uptime_seconds": round(now - _started_at),
        "total_requests": total_req,
        "total_errors": total_err,
        "success_rate_pct": round((1 - total_err / total_req) * 100, 1) if total_req else 100.0,
        "tokens": {
            "prompt": sum(m.prompt_tokens for m in all_ms),
            "completion": sum(m.completion_tokens for m in all_ms),
            "total": sum(m.total_tokens for m in all_ms),
        },
        "latency_ms": {
            "avg": round(sum(all_latencies) / len(all_latencies), 1) if all_latencies else None,
            "p50": _pct(0.50),
            "p90": _pct(0.90),
            "p99": _pct(0.99),
        },
        "throughput": {"rpm": rpm, "rph": rph},
        "by_provider": providers,
    }


def get_recent_requests(limit: int = 50, provider: str = "", model: str = "") -> list[dict]:
    history = list(_history)
    history.reverse()  # newest first
    if provider:
        history = [r for r in history if r.provider == provider]
    if model:
        history = [r for r in history if r.model == model]
    return [r.to_dict() for r in history[:limit]]
