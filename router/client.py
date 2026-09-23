"""
Unified router client — OpenRouter-style.

Routes requests to the correct backend based on model ID prefix.
Calls each backend's internal client directly — no HTTP hops.

Model routing:
  claude-*                       → claude_proxy  (claude.ai session)
  gemini-flash*, gemini-pro*     → gemini_proxy  (Google cookies)
  gpt-*, o1*, o3*                → chatgpt_proxy (chatgpt.com session)
  k2*, kimi-*, moonshot-*        → kimi_proxy    (kimi.ai refresh token)
"""
from __future__ import annotations

# ── Model registry ────────────────────────────────────────────────────────────

MODELS: list[dict] = [
    # Claude
    {"id": "claude-fable-5-1",   "backend": "claude",  "owned_by": "anthropic"},
    {"id": "claude-opus-5",      "backend": "claude",  "owned_by": "anthropic"},
    {"id": "claude-sonnet-5",    "backend": "claude",  "owned_by": "anthropic"},
    {"id": "claude-opus-4-6",    "backend": "claude",  "owned_by": "anthropic"},
    {"id": "claude-sonnet-4-6",  "backend": "claude",  "owned_by": "anthropic"},
    {"id": "claude-opus-4-5",    "backend": "claude",  "owned_by": "anthropic"},
    {"id": "claude-haiku-4-5",   "backend": "claude",  "owned_by": "anthropic"},
    # Gemini (verified locally against gemini_webapi session)
    {"id": "gemini-3-flash",      "backend": "gemini", "owned_by": "google"},
    {"id": "gemini-3-pro",        "backend": "gemini", "owned_by": "google"},
    {"id": "gemini-3-flash-lite", "backend": "gemini", "owned_by": "google"},
    # ChatGPT (verified against the account's live /backend-api/models list)
    {"id": "gpt-6-astra-wm",     "backend": "chatgpt", "owned_by": "openai"},
    {"id": "gpt-5.6-terra-wm",   "backend": "chatgpt", "owned_by": "openai"},
    {"id": "gpt-5.6-sol-wm",     "backend": "chatgpt", "owned_by": "openai"},
    {"id": "gpt-5.6-luna-wm",    "backend": "chatgpt", "owned_by": "openai"},
    {"id": "gpt-5.5-wm",         "backend": "chatgpt", "owned_by": "openai"},
    {"id": "gpt-5-5",            "backend": "chatgpt", "owned_by": "openai"},
    {"id": "gpt-5-5-instant",    "backend": "chatgpt", "owned_by": "openai"},
    {"id": "gpt-5-6",            "backend": "chatgpt", "owned_by": "openai"},
    {"id": "gpt-5-6-instant",    "backend": "chatgpt", "owned_by": "openai"},
    {"id": "gpt-5-3-mini",       "backend": "chatgpt", "owned_by": "openai"},
    {"id": "gpt-5-5-mini",       "backend": "chatgpt", "owned_by": "openai"},
    {"id": "gpt-5-6-mini",       "backend": "chatgpt", "owned_by": "openai"},
    {"id": "gpt-5-4-t-mini",     "backend": "chatgpt", "owned_by": "openai"},
    {"id": "gpt-5-6-t-mini",     "backend": "chatgpt", "owned_by": "openai"},
    {"id": "gpt-5-5-thinking",   "backend": "chatgpt", "owned_by": "openai"},
    {"id": "gpt-5-6-thinking",   "backend": "chatgpt", "owned_by": "openai"},
    {"id": "research",           "backend": "chatgpt", "owned_by": "openai"},
    # Legacy slugs — not in the current model switcher, but still functionally
    # routable when called explicitly (verified live), kept for compatibility.
    {"id": "gpt-4o",             "backend": "chatgpt", "owned_by": "openai"},
    {"id": "gpt-4o-mini",        "backend": "chatgpt", "owned_by": "openai"},
    {"id": "o1-mini",            "backend": "chatgpt", "owned_by": "openai"},
    {"id": "o3-mini",            "backend": "chatgpt", "owned_by": "openai"},
    # Kimi
    {"id": "k2d6-chat",          "backend": "kimi",    "owned_by": "moonshot"},
    {"id": "k2-chat",            "backend": "kimi",    "owned_by": "moonshot"},
    {"id": "k2-0711-preview",    "backend": "kimi",    "owned_by": "moonshot"},
    {"id": "kimi-latest",        "backend": "kimi",    "owned_by": "moonshot"},
    {"id": "k1.5",               "backend": "kimi",    "owned_by": "moonshot"},
    {"id": "moonshot-v1-8k",     "backend": "kimi",    "owned_by": "moonshot"},
    {"id": "moonshot-v1-32k",    "backend": "kimi",    "owned_by": "moonshot"},
    {"id": "moonshot-v1-128k",   "backend": "kimi",    "owned_by": "moonshot"},
]

_MODEL_MAP = {m["id"]: m for m in MODELS}

# claude-webapi model aliases (short → full versioned ID)
_CLAUDE_ALIASES = {
    "claude-haiku-4-5":  "claude-haiku-4-5-20251001",
    "claude-opus-4-5":   "claude-opus-4-5-20251101",
    "claude-sonnet-4-6": "claude-sonnet-4-6",
    "claude-opus-4-6":   "claude-opus-4-6",
    "claude-sonnet-5":   "claude-sonnet-5",
    "claude-opus-5":     "claude-opus-5",
}


def _route(model: str) -> str:
    if model in _MODEL_MAP:
        return _MODEL_MAP[model]["backend"]
    m = model.lower()
    if m.startswith("claude"):
        return "claude"
    if m.startswith("gemini"):
        return "gemini"
    if m.startswith(("gpt", "o1", "o3", "o4", "chatgpt")):
        return "chatgpt"
    if m.startswith(("k2", "kimi", "moonshot")):
        return "kimi"
    raise ValueError(f"Unknown model '{model}' — cannot route to any backend")


async def ask(prompt: str, model: str, user_id: str = "", files: list[bytes] | None = None,
              _track: bool = True) -> tuple[str, str]:
    """
    Route prompt to the correct backend.
    Returns (response_text, backend_name).
    Records latency + success/fail into health + stats trackers automatically.
    Pass _track=False to suppress stats/DB recording (used by health pings).
    """
    import time
    from router.health import record as health_record
    from router import stats as _stats

    backend = _route(model)
    t = time.time()
    if _track:
        print(f"[ask] → {backend} model={model} prompt={len(prompt)} chars files={len(files or [])} user={user_id or '-'}")

    try:
        if backend == "claude":
            from claude_proxy.client import get
            resolved = _CLAUDE_ALIASES.get(model, model)
            r = await get().generate_content(prompt, model=resolved, files=files or None)
            text = r.text

        elif backend == "gemini":
            import tempfile, pathlib, imghdr
            from gemini_proxy.client import get as get_gemini
            # gemini_webapi uploads raw bytes as .txt → wrong MIME; write to temp files with correct extension
            send_files = None
            tmp = None
            if files:
                tmp = tempfile.TemporaryDirectory()
                send_files = []
                for i, f in enumerate(files):
                    ext = imghdr.what(None, h=f) or "jpg"
                    p = pathlib.Path(tmp.name) / f"img_{i}.{ext}"
                    p.write_bytes(f)
                    send_files.append(p)
            try:
                r = await get_gemini().generate_content(prompt, files=send_files, model=model, temporary=True)
            finally:
                if tmp:
                    tmp.cleanup()
            text = r.text

        elif backend == "chatgpt":
            from chatgpt_proxy.client import ask as chatgpt_ask
            text = await chatgpt_ask(prompt, model=model, files=files or None)

        elif backend == "kimi":
            from kimi_proxy.client import ask as kimi_ask
            text = await kimi_ask(prompt, model=model)

        else:
            raise RuntimeError(f"Unhandled backend: {backend}")

        latency_ms = (time.time() - t) * 1000
        health_record(backend, latency_ms, True)
        if _track:
            print(f"[ask] ✓ {backend} model={model} {latency_ms/1000:.1f}s reply={len(text)} chars")
            _stats.record(backend, model, prompt, text, latency_ms, True, user_id=user_id)
        return text, backend

    except Exception as e:
        latency_ms = (time.time() - t) * 1000
        err = str(e)[:120]
        if _track:
            print(f"[ask] ✗ {backend} model={model} after {latency_ms/1000:.1f}s: {e!r}")
        health_record(backend, latency_ms, False, err)
        if _track:
            _stats.record(backend, model, prompt, "", latency_ms, False, err, user_id=user_id)
        raise
