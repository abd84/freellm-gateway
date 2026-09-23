"""
Unified AI Router — OpenRouter-style local proxy.

Backends: claude · gemini · chatgpt · kimi
Run:  uvicorn router.main:app --port 8000 --reload

Endpoints:
  POST /v1/chat/completions       OpenAI SDK-compatible
  POST /generate/text             Simple { prompt, model } → { text, backend, latency_ms }
  GET  /models                    All models with metadata (context, capabilities)
  GET  /providers                 Per-provider health, latency, uptime, error rate
  GET  /health                    Quick status summary of all providers
  GET  /health/{provider}         Single provider detail
"""
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

load_dotenv()

from router.client import MODELS, ask
from router import auth as _auth
from router import config as _config
from router import db as _db
from router import health as _health
from router import security as _security
from router import stats as _stats


# ── Model metadata (mirrors OpenRouter schema) ────────────────────────────────

_MODEL_META: dict[str, dict] = {
    # Claude
    "claude-fable-5-1":   {"context_length": 200000, "max_output": 32000,  "capabilities": ["text", "reasoning", "code", "analysis", "vision"]},
    "claude-opus-5":      {"context_length": 200000, "max_output": 32000,  "capabilities": ["text", "reasoning", "code", "analysis", "vision"]},
    "claude-sonnet-5":    {"context_length": 200000, "max_output": 16000,  "capabilities": ["text", "reasoning", "code", "vision"]},
    "claude-opus-4-6":    {"context_length": 200000, "max_output": 32000,  "capabilities": ["text", "reasoning", "code", "analysis"]},
    "claude-sonnet-4-6":  {"context_length": 200000, "max_output": 16000,  "capabilities": ["text", "reasoning", "code"]},
    "claude-opus-4-5":    {"context_length": 200000, "max_output": 32000,  "capabilities": ["text", "reasoning", "code", "analysis"]},
    "claude-haiku-4-5":   {"context_length": 200000, "max_output": 8000,   "capabilities": ["text", "code"]},
    # Gemini (verified locally against gemini_webapi session)
    "gemini-3-flash":      {"context_length": 1000000, "max_output": 8192, "capabilities": ["text", "vision", "image_gen"]},
    "gemini-3-pro":        {"context_length": 2000000, "max_output": 8192, "capabilities": ["text", "vision", "reasoning", "image_gen"]},
    "gemini-3-flash-lite": {"context_length": 1000000, "max_output": 8192, "capabilities": ["text", "vision"]},
    # ChatGPT
    "gpt-6-astra-wm":     {"context_length": 256000,  "max_output": 32768, "capabilities": ["text", "reasoning", "code", "vision", "image_gen"]},
    "gpt-5.6-terra-wm":   {"context_length": 256000,  "max_output": 32768, "capabilities": ["text", "reasoning", "code", "vision"]},
    "gpt-5.6-sol-wm":     {"context_length": 256000,  "max_output": 32768, "capabilities": ["text", "reasoning", "code", "vision"]},
    "gpt-5.6-luna-wm":    {"context_length": 256000,  "max_output": 32768, "capabilities": ["text", "reasoning", "code", "vision"]},
    "gpt-5-5-wm":         {"context_length": 256000,  "max_output": 32768, "capabilities": ["text", "reasoning", "code", "vision"]},
    "gpt-5-5":            {"context_length": 256000,  "max_output": 32768, "capabilities": ["text", "reasoning", "code", "vision"]},
    "gpt-5-6":            {"context_length": 256000,  "max_output": 32768, "capabilities": ["text", "reasoning", "code", "vision"]},
    "gpt-5-3-mini":       {"context_length": 128000,  "max_output": 16384, "capabilities": ["text", "code"]},
    "gpt-5-5-mini":       {"context_length": 128000,  "max_output": 16384, "capabilities": ["text", "code"]},
    "gpt-5-5-thinking":   {"context_length": 256000,  "max_output": 32768, "capabilities": ["text", "reasoning", "code"]},
    "gpt-4o":             {"context_length": 128000,  "max_output": 16384, "capabilities": ["text", "reasoning", "code", "vision"]},
    "gpt-4o-mini":        {"context_length": 128000,  "max_output": 16384, "capabilities": ["text", "code"]},
    "o1-mini":            {"context_length": 128000,  "max_output": 65536, "capabilities": ["text", "reasoning", "code"]},
    "o3-mini":            {"context_length": 200000,  "max_output": 65536, "capabilities": ["text", "reasoning", "code"]},
    # Kimi
    "k2d6-chat":          {"context_length": 128000,  "max_output": 8192,  "capabilities": ["text", "code", "reasoning"]},
    "k2-chat":            {"context_length": 128000,  "max_output": 8192,  "capabilities": ["text", "code", "reasoning"]},
    "k2-0711-preview":    {"context_length": 128000,  "max_output": 8192,  "capabilities": ["text", "code", "reasoning"]},
    "kimi-latest":        {"context_length": 128000,  "max_output": 8192,  "capabilities": ["text", "code", "reasoning"]},
    "k1.5":               {"context_length": 128000,  "max_output": 8192,  "capabilities": ["text", "reasoning"]},
    "moonshot-v1-8k":     {"context_length": 8192,    "max_output": 4096,  "capabilities": ["text"]},
    "moonshot-v1-32k":    {"context_length": 32768,   "max_output": 8192,  "capabilities": ["text"]},
    "moonshot-v1-128k":   {"context_length": 131072,  "max_output": 8192,  "capabilities": ["text", "long_context"]},
}

_PROVIDER_INFO = {
    "claude":  {"name": "Anthropic Claude",  "url": "claude.ai",      "auth": "session_cookie", "free": True},
    "gemini":  {"name": "Google Gemini",     "url": "gemini.google.com", "auth": "1psid_cookies", "free": True},
    "chatgpt": {"name": "OpenAI ChatGPT",    "url": "chatgpt.com",    "auth": "session_cookie", "free": True},
    "kimi":    {"name": "Moonshot Kimi",     "url": "kimi.ai",        "auth": "refresh_token",  "free": True},
}


# ── Startup ───────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(_: FastAPI):
    _config.validate()
    _security.init_access_logger()
    _db.init_db()

    failed = []

    for backend, init_mod, init_fn in [
        ("claude",  "claude_proxy.client",  "init_client"),
        ("gemini",  "gemini_proxy.client",  "init_client"),
        ("chatgpt", "chatgpt_proxy.client", "init_client"),
        ("kimi",    "kimi_proxy.client",    "init_client"),
    ]:
        try:
            import importlib
            m = importlib.import_module(init_mod)
            await getattr(m, init_fn)()
        except Exception as e:
            print(f"Router: {backend} init failed: {e}")
            failed.append(backend)

    if failed:
        print(f"Router: degraded — failed backends: {failed}")
    else:
        print("Router: all backends ready")

    # Initial health ping + start background pings
    await _health.run_initial_ping()
    _health.start_background_pings()

    yield

    _health.stop_background_pings()
    for close_mod, close_fn in [
        ("claude_proxy.client",  "close_client"),
        ("gemini_proxy.client",  "close_client"),
        ("chatgpt_proxy.client", "close_client"),
        ("kimi_proxy.client",    "close_client"),
    ]:
        try:
            import importlib
            m = importlib.import_module(close_mod)
            await getattr(m, close_fn)()
        except Exception:
            pass


app = FastAPI(title="AI Router", lifespan=lifespan)

_static_dir = Path(__file__).parent / "static"
if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")

# Security middleware (headers, logging, request size, brute force)
app.add_middleware(_security.SecurityMiddleware)

# CORS (configured via CORS_ORIGINS env var)
_cors_origins = _security.get_cors_origins()
if _cors_origins:
    # "*" + allow_credentials=True is an invalid/unsafe combo per the CORS spec
    # (browsers reject it for credentialed requests) — never send both.
    _cors_wildcard = "*" in _cors_origins
    if _cors_wildcard:
        print("WARNING: CORS_ORIGINS=* — allowing all origins WITHOUT credentials. "
              "Set explicit origins if you need cookies/Authorization headers to work cross-origin.")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=not _cors_wildcard,
        allow_methods=["*"],
        allow_headers=["*"],
    )


# ── Root ──────────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {
        "service": "ai-router",
        "backends": list(_PROVIDER_INFO.keys()),
        "total_models": len(MODELS),
        "endpoints": {
            "models":           "GET  /models",
            "providers":        "GET  /providers",
            "health":           "GET  /health",
            "health_provider":  "GET  /health/{provider}",
            "chat":             "POST /v1/chat/completions",
            "generate":         "POST /generate/text",
            "stats":            "GET  /stats",
            "stats_providers":  "GET  /stats/providers",
            "stats_provider":   "GET  /stats/providers/{provider}",
            "stats_models":     "GET  /stats/models",
            "stats_model":      "GET  /stats/models/{model}",
            "requests":         "GET  /requests?limit=50&provider=&model=",
            "chat":             "GET  /chat",
            "dashboard":        "GET  /dashboard",
            "admin_users":      "POST/GET /admin/users",
            "admin_user":       "GET/DELETE /admin/users/{user_id}",
            "me":               "GET  /me",
            "me_requests":      "GET  /me/requests",
        },
    }


# ── Models ────────────────────────────────────────────────────────────────────

@app.get("/models")
@app.get("/v1/models")
def list_models():
    data = []
    for m in MODELS:
        meta = _MODEL_META.get(m["id"], {})
        h = _health.get_one(m["backend"]) or {}
        data.append({
            "id":             m["id"],
            "object":         "model",
            "owned_by":       m["owned_by"],
            "backend":        m["backend"],
            "context_length": meta.get("context_length"),
            "max_output":     meta.get("max_output"),
            "capabilities":   meta.get("capabilities", ["text"]),
            "status":         h.get("status", "unknown"),
            "avg_latency_ms": h.get("avg_latency_ms"),
        })
    return {"object": "list", "data": data}


# ── Providers ─────────────────────────────────────────────────────────────────

@app.get("/providers")
def list_providers():
    result = {}
    for backend, info in _PROVIDER_INFO.items():
        h = _health.get_one(backend) or {}
        models = [m["id"] for m in MODELS if m["backend"] == backend]
        result[backend] = {
            **info,
            "models": models,
            "status":            h.get("status", "unknown"),
            "avg_latency_ms":    h.get("avg_latency_ms"),
            "avg_latency_ms_image": h.get("avg_latency_ms_image"),
            "p90_latency_ms":    h.get("p90_latency_ms"),
            "p90_latency_ms_image": h.get("p90_latency_ms_image"),
            "error_rate_pct":    h.get("error_rate_pct", 0),
            "uptime_pct":        h.get("uptime_pct", 100),
            "total_requests":    h.get("total_requests", 0),
            "total_errors":      h.get("total_errors", 0),
            "last_error":        h.get("last_error"),
            "last_ping_at":      h.get("last_ping_at"),
            "last_ping_ok":      h.get("last_ping_ok"),
            "last_ping_latency_ms": h.get("last_ping_latency_ms"),
        }
    return result


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health_summary():
    all_stats = _health.get_all()
    overall = "up"
    for h in all_stats.values():
        if h["status"] == "down":
            overall = "degraded"
            break
        if h["status"] == "degraded" and overall == "up":
            overall = "degraded"
    return {
        "status": overall,
        "providers": {
            name: {
                "status":               h["status"],
                "avg_latency_ms":       h["avg_latency_ms"],
                "avg_latency_ms_image": h["avg_latency_ms_image"],
                "uptime_pct":           h["uptime_pct"],
                "error_rate_pct":       h["error_rate_pct"],
                "last_ping_ok":         h["last_ping_ok"],
            }
            for name, h in all_stats.items()
        },
        "ping_interval_sec": _health.PING_INTERVAL,
        "tracking_window":   _health.WINDOW,
    }


@app.get("/health/{provider}")
def health_provider(provider: str):
    h = _health.get_one(provider)
    if h is None:
        raise HTTPException(404, f"Unknown provider '{provider}'. Valid: claude, gemini, chatgpt, kimi")
    info = _PROVIDER_INFO.get(provider, {})
    models = [m["id"] for m in MODELS if m["backend"] == provider]
    return {"provider": provider, **info, "models": models, **h}


# ── Simple endpoint ───────────────────────────────────────────────────────────

class TextRequest(BaseModel):
    prompt: str
    model: str

    @field_validator("prompt")
    @classmethod
    def prompt_max_length(cls, v: str) -> str:
        if len(v) > 100_000:
            raise ValueError("Prompt exceeds 100,000 character limit")
        return v

    @field_validator("model")
    @classmethod
    def model_clean(cls, v: str) -> str:
        v = v.strip()
        if len(v) > 50:
            raise ValueError("Model name exceeds 50 character limit")
        return v


@app.post("/generate/text")
async def generate_text(req: TextRequest, user: dict = Depends(_auth.get_current_user)):
    uid = user["id"]
    t = time.time()
    try:
        text, backend = await ask(req.prompt, req.model, user_id=uid)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))
    return {
        "text":       text,
        "model":      req.model,
        "backend":    backend,
        "latency_ms": round((time.time() - t) * 1000),
    }


# ── OpenAI-compatible endpoint ────────────────────────────────────────────────

_MAX_FETCHED_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB


async def _is_public_host(host: str) -> bool:
    """Reject hosts resolving to private/loopback/link-local/reserved IPs —
    blocks SSRF against cloud metadata endpoints, localhost, and internal
    services via an attacker-supplied image_url."""
    import asyncio
    import ipaddress
    import socket
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, None)
    except socket.gaierror:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return False
    return True


async def _extract_images(body: dict) -> list[bytes]:
    """Pull image bytes from image_url content parts (data URIs or HTTP URLs)."""
    import base64 as _b64
    from urllib.parse import urlparse
    import httpx as _httpx
    images = []
    for msg in body.get("messages", []):
        content = msg.get("content", "")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "image_url":
                continue
            url = (part.get("image_url") or {}).get("url", "")
            if url.startswith("data:"):
                # data:image/png;base64,<data>
                try:
                    images.append(_b64.b64decode(url.split(",", 1)[1]))
                except Exception:
                    pass
            elif url.startswith("http"):
                host = urlparse(url).hostname
                if not host or not await _is_public_host(host):
                    print(f"[_extract_images] blocked fetch of non-public host: {url[:100]!r}")
                    continue
                try:
                    async with _httpx.AsyncClient(timeout=10, follow_redirects=False) as c:
                        async with c.stream("GET", url) as r:
                            r.raise_for_status()
                            buf = bytearray()
                            async for chunk in r.aiter_bytes():
                                buf += chunk
                                if len(buf) > _MAX_FETCHED_IMAGE_BYTES:
                                    raise ValueError("fetched image exceeds size limit")
                            images.append(bytes(buf))
                except Exception:
                    pass
    return images


def _flatten(body: dict) -> str:
    parts = []
    system = body.get("system")
    if system:
        s = system if isinstance(system, str) else " ".join(b.get("text", "") for b in system if isinstance(b, dict))
        parts.append(f"System: {s}")
    for msg in body.get("messages", []):
        role = msg.get("role", "user")
        content = msg.get("content") or ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
        else:
            text = ""
        prefix = {"user": "Human", "assistant": "Assistant", "system": "System"}.get(role, role.capitalize())
        parts.append(f"{prefix}: {text}")
    return "\n\n".join(parts)


# Backends return the full text only once done — Fable/Opus on long tasks can take
# minutes. With nothing on the wire, clients and nginx hit idle timeouts, so stream
# requests open the response immediately and send a heartbeat every few seconds.
_HEARTBEAT_S = 10
_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}  # no nginx buffering


def _check_model(model: str) -> None:
    """Reject unknown models with a 400 before a 200 stream is started."""
    from router.client import _route
    try:
        _route(model)
    except ValueError as e:
        raise HTTPException(400, str(e))


async def _ask_with_heartbeat(prompt, model, uid, images, ping: str):
    """Yield `ping` every _HEARTBEAT_S while ask() runs. The last item is
    (text,) on success or the Exception on failure."""
    import asyncio
    t0 = time.time()
    print(f"[stream] {model} opened — heartbeat every {_HEARTBEAT_S}s")
    task = asyncio.create_task(ask(prompt, model, user_id=uid, files=images or None))
    pings = 0
    try:
        while not (await asyncio.wait({task}, timeout=_HEARTBEAT_S))[0]:
            yield ping
            pings += 1
            if pings % 6 == 0:  # ~once a minute
                print(f"[stream] {model} still waiting — {time.time() - t0:.0f}s, {pings} heartbeats sent")
        try:
            text = task.result()[0]
            print(f"[stream] {model} sending reply — {time.time() - t0:.1f}s, {len(text)} chars, {pings} heartbeats")
            yield (text,)
        except Exception as e:
            import traceback as _tb
            print(f"[stream 500] model={model} after {time.time() - t0:.1f}s error={e!r}\n{_tb.format_exc()}")
            yield e
    finally:
        if not task.done():
            print(f"[stream] {model} client disconnected after {time.time() - t0:.1f}s — cancelling backend call")
        task.cancel()  # no-op if done; stops the backend call if the client disconnected


@app.post("/v1/chat/completions")
async def chat_completions(request: Request, user: dict = Depends(_auth.get_current_user)):
    import json as _json
    uid = user["id"]
    body = await request.json()
    model = body.get("model")
    if not model:
        raise HTTPException(400, "model is required")
    model = str(model).strip()
    if len(model) > 50:
        raise HTTPException(400, "Model name exceeds 50 character limit")
    prompt = _flatten(body)
    if not prompt.strip():
        raise HTTPException(400, "No message content")
    if len(prompt) > 100_000:
        raise HTTPException(400, "Prompt exceeds 100,000 character limit")
    images = await _extract_images(body)

    # Streaming: open the response now and send keepalives while the backend works,
    # then emit the full text as a single SSE chunk (see _ask_with_heartbeat).
    if body.get("stream"):
        _check_model(model)
        cid = f"router-{int(time.time()*1000)}"

        async def _sse():
            async for item in _ask_with_heartbeat(prompt, model, uid, images, ": keepalive\n\n"):
                if isinstance(item, str):
                    yield item
            if isinstance(item, Exception):
                yield f"data: {_json.dumps({'error': {'message': str(item), 'type': 'server_error'}})}\n\n"
                yield "data: [DONE]\n\n"
                return
            text = item[0]
            usage = {
                "prompt_tokens":     max(1, len(prompt) // 4),
                "completion_tokens": max(1, len(text) // 4),
                "total_tokens":      max(1, (len(prompt) + len(text)) // 4),
            }
            chunk = {
                "id": cid, "object": "chat.completion.chunk", "model": model,
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}],
            }
            yield f"data: {_json.dumps(chunk)}\n\n"
            done = {
                "id": cid, "object": "chat.completion.chunk", "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": usage,
            }
            yield f"data: {_json.dumps(done)}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(_sse(), media_type="text/event-stream", headers=_SSE_HEADERS)

    t = time.time()
    try:
        text, backend = await ask(prompt, model, user_id=uid, files=images or None)
    except ValueError as e:
        print(f"[chat/completions 400] model={model} error={e}")
        raise HTTPException(400, str(e))
    except Exception as e:
        import traceback as _tb
        print(f"[chat/completions 500] model={model} error={e!r}\n{_tb.format_exc()}")
        raise HTTPException(500, str(e))

    latency = round((time.time() - t) * 1000)
    cid = f"router-{int(time.time()*1000)}"
    usage = {
        "prompt_tokens":     max(1, len(prompt) // 4),
        "completion_tokens": max(1, len(text) // 4),
        "total_tokens":      max(1, (len(prompt) + len(text)) // 4),
    }

    return {
        "id": cid, "object": "chat.completion", "model": model, "backend": backend,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": usage,
        "latency_ms": latency,
    }


# ── Anthropic-compatible endpoint (for Claude Code) ──────────────────────────

@app.post("/v1/messages")
async def messages(request: Request, user: dict = Depends(_auth.get_current_user)):
    import json as _json
    uid = user["id"]
    body = await request.json()
    model = str(body.get("model", "")).strip()
    if not model:
        raise HTTPException(400, "model is required")
    if len(model) > 50:
        raise HTTPException(400, "Model name exceeds 50 character limit")

    prompt = _flatten(body)
    if not prompt.strip():
        raise HTTPException(400, "No message content")
    if len(prompt) > 100_000:
        raise HTTPException(400, "Prompt exceeds 100,000 character limit")
    images = await _extract_images(body)

    msg_id = f"msg_{int(time.time()*1000)}"
    in_tok  = max(1, len(prompt) // 4)

    # Streaming: send message_start + pings while the backend works (see _ask_with_heartbeat)
    if body.get("stream"):
        _check_model(model)
        ping = f"event: ping\ndata: {_json.dumps({'type':'ping'})}\n\n"

        async def _sse():
            yield f"event: message_start\ndata: {_json.dumps({'type':'message_start','message':{'id':msg_id,'type':'message','role':'assistant','content':[],'model':model,'stop_reason':None,'stop_sequence':None,'usage':{'input_tokens':in_tok,'output_tokens':1}}})}\n\n"
            async for item in _ask_with_heartbeat(prompt, model, uid, images, ping):
                if isinstance(item, str):
                    yield item
            if isinstance(item, Exception):
                yield f"event: error\ndata: {_json.dumps({'type':'error','error':{'type':'api_error','message':str(item)}})}\n\n"
                return
            text = item[0]
            out_tok = max(1, len(text) // 4)
            yield f"event: content_block_start\ndata: {_json.dumps({'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}})}\n\n"
            yield f"event: content_block_delta\ndata: {_json.dumps({'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':text}})}\n\n"
            yield f"event: content_block_stop\ndata: {_json.dumps({'type':'content_block_stop','index':0})}\n\n"
            yield f"event: message_delta\ndata: {_json.dumps({'type':'message_delta','delta':{'stop_reason':'end_turn','stop_sequence':None},'usage':{'output_tokens':out_tok}})}\n\n"
            yield f"event: message_stop\ndata: {_json.dumps({'type':'message_stop'})}\n\n"
        return StreamingResponse(_sse(), media_type="text/event-stream", headers=_SSE_HEADERS)

    try:
        text, backend = await ask(prompt, model, user_id=uid, files=images or None)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        import traceback as _tb
        print(f"[messages 500] model={model} error={e!r}\n{_tb.format_exc()}")
        raise HTTPException(500, str(e))

    out_tok = max(1, len(text) // 4)

    return {
        "id": msg_id,
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "model": model,
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": in_tok, "output_tokens": out_tok},
    }


# ── Image generation ─────────────────────────────────────────────────────────

class ImageGenRequest(BaseModel):
    prompt: str
    model: str = "gemini-3.8-flash"
    n: int = Field(1, ge=1, le=4)
    response_format: str = "b64_json"  # "b64_json" or "url"

async def _gemini_images(prompt, files, model, n, response_format):
    import asyncio, imghdr as _imghdr
    import time as _time, base64 as _b64, tempfile as _tmp
    from pathlib import Path

    # gemini_webapi accepts: gemini-flash, gemini-flash-lite, gemini-pro
    _VALID = {"gemini-flash", "gemini-flash-lite", "gemini-pro"}
    _webapi_model = model if model in _VALID else "gemini-pro"
    _req_start = _time.time()
    _file_count = len(files) if files else 0
    print(f"[gemini_images] START model={_webapi_model} n={n} files={_file_count} fmt={response_format} prompt={prompt[:80]!r}")

    try:
        from gemini_proxy.client import get as get_gemini, rotate as rotate_gemini, pool_size as gemini_pool_size
        from gemini_webapi.constants import Headers

        # Input files need temp files with correct extension so MIME type is detected correctly.
        # Raw bytes lose the extension → application/octet-stream → Gemini ignores as image.
        tmp = send_files = None
        _setup_start = _time.time()
        if files:
            tmp = _tmp.TemporaryDirectory()
            send_files = []
            for i, f in enumerate(files):
                ext = _imghdr.what(None, h=f) or "png"
                p = Path(tmp.name) / f"img_{i}.{ext}"
                p.write_bytes(f)
                send_files.append(p)
            _setup_ms = round((_time.time() - _setup_start) * 1000)
            print(f"[gemini_images] file prep: {[p.name for p in send_files]} ({_setup_ms}ms)")

        async def _one_image(idx: int):
            _t0 = _time.time()
            print(f"[gemini_images] → [{idx+1}/{n}] sending to Gemini — waiting for response...")
            r = await asyncio.wait_for(
                get_gemini().generate_content(prompt, files=send_files, model=_webapi_model, temporary=True),
                timeout=300,
            )
            _generate_s = round(_time.time() - _t0, 2)
            if not r.images:
                _txt = (r.text or "")[:200]
                print(f"[gemini_images] ✗ [{idx+1}/{n}] no images ({_generate_s}s). gemini said: {_txt!r}")
                raise ValueError(f"No images returned — gemini text: {_txt!r}")
            img = r.images[0]
            print(f"[gemini_images] ✓ [{idx+1}/{n}] generate_content={_generate_s}s | url={img.url[:60]}")
            if response_format == "url":
                return {"url": img.url}
            # Fetch bytes directly from CDN URL — skip save()/disk I/O
            url = img.url
            if "=s" not in url:
                url += "=s2048-rj"
            else:
                url = url.split("=s")[0] + "=s2048-rj"
            _fetch_t = _time.time()
            resp = await get_gemini()._live_client.get(url, headers=Headers.REFERER.value)
            _fetch_s = round(_time.time() - _fetch_t, 2)
            _total_s = round(_time.time() - _t0, 2)
            _kb = round(len(resp.content) / 1024, 1)
            print(
                f"[gemini_images] ✓ [{idx+1}/{n}] done {_kb}KB | "
                f"generate={_generate_s}s  cdn_fetch={_fetch_s}s  total={_total_s}s"
            )
            return {"b64_json": _b64.b64encode(resp.content).decode()}

        try:
            _last_err = None
            results = None
            _pool_n = gemini_pool_size()
            for _attempt in range(_pool_n):
                _acct_idx = _attempt + 1
                _attempt_start = _time.time()
                print(f"[gemini_images] attempt {_acct_idx}/{_pool_n} — dispatching {n} image(s) in parallel")
                try:
                    results = await asyncio.gather(*[_one_image(i) for i in range(n)])
                    _attempt_s = round(_time.time() - _attempt_start, 2)
                    print(f"[gemini_images] ✓ account {_acct_idx} succeeded in {_attempt_s}s")
                    break
                except ValueError as _ve:
                    _attempt_s = round(_time.time() - _attempt_start, 2)
                    _last_err = _ve
                    print(f"[gemini_images] ✗ account {_acct_idx} failed after {_attempt_s}s: {_ve}")
                    if _attempt < _pool_n - 1:
                        rotate_gemini()
                    else:
                        print(f"[gemini_images] ✗ all {_pool_n} accounts failed")
            if results is None:
                raise _last_err or ValueError("All Gemini accounts failed")
        finally:
            if tmp:
                tmp.cleanup()

        _total_s = round(_time.time() - _req_start, 2)
        _queue_s = round(_time.time() - _setup_start - (_total_s - (_time.time() - _req_start)), 2) if files else 0
        print(f"[gemini_images] DONE {n} image(s) total={_total_s}s (from request entry)")
        return {"created": int(_time.time()), "data": list(results)}

    except asyncio.TimeoutError:
        print(f"[gemini_images] ✗ TIMEOUT after {round(_time.time()-_req_start,1)}s")
        raise HTTPException(504, "Gemini image generation timed out — cookies may be expired")
    except Exception as _we:
        print(f"[gemini_images] ✗ ERROR after {round(_time.time()-_req_start,1)}s: {_we}")
        raise HTTPException(502, f"Gemini image generation failed: {_we}")


async def _chatgpt_images(prompt: str, model: str, n: int, files: list[bytes] | None = None) -> list[dict]:
    """
    Dispatch n ChatGPT image requests (generation, or editing if `files` is
    given) in parallel against the current pooled account. Mirrors
    _gemini_images' pool pattern: on total failure of the current account,
    rotate to the next and retry the whole batch, up to pool_size() attempts.
    Rotation only happens between attempts, never mid-flight, so the n
    parallel requests within one attempt never race against a pointer change.
    """
    import asyncio, re as _re, base64 as _b64
    from chatgpt_proxy.client import ask as chatgpt_ask, fetch_image_bytes, rotate as rotate_chatgpt, pool_size as chatgpt_pool_size

    async def _one(idx: int) -> dict:
        text = await chatgpt_ask(prompt, model=model, keep_history=True, files=files)
        urls = _re.findall(r'!\[.*?\]\((https?://[^)]+)\)', text)
        if not urls:
            raise ValueError(f"No image returned — ChatGPT said: {text[:200]!r}")
        content = await fetch_image_bytes(urls[0])
        return {"b64_json": _b64.b64encode(content).decode()}

    _pool_n = max(1, chatgpt_pool_size())
    _last_err: Exception | None = None
    for _attempt in range(_pool_n):
        _acct_idx = _attempt + 1
        print(f"[chatgpt_images] attempt {_acct_idx}/{_pool_n} — dispatching {n} image(s) in parallel")
        try:
            return list(await asyncio.gather(*[_one(i) for i in range(n)]))
        except Exception as e:
            _last_err = e
            print(f"[chatgpt_images] ✗ account {_acct_idx} failed: {e}")
            if _attempt < _pool_n - 1:
                rotate_chatgpt()
            else:
                print(f"[chatgpt_images] ✗ all {_pool_n} account(s) failed")
    raise _last_err


@app.post("/v1/images/generations")
async def image_generations(req: ImageGenRequest, user: dict = Depends(_auth.get_current_user)):
    uid = user["id"]
    t = time.time()

    # ChatGPT/DALL-E path
    if req.model.startswith(("gpt-", "dall-e")):
        try:
            # DALL-E is invoked as a tool from a real model's conversation turn, not
            # picked directly — "dall-e-3"/"dall-e-2"/"gpt-image-1" aren't real
            # chatgpt.com chat models, so those fall back to gpt-4o. A real chat
            # model (gpt-4o, sol, astra, etc. — verified all trigger the tool fine)
            # is used as-is. keep_history=True (set inside _chatgpt_images) is
            # required: ChatGPT disables image generation in temporary/incognito chats.
            _chatgpt_model_ids = {m["id"] for m in MODELS if m["backend"] == "chatgpt"}
            _underlying_model = req.model if req.model in _chatgpt_model_ids else "gpt-4o"
            data = await _chatgpt_images(f"Generate an image: {req.prompt}", _underlying_model, req.n)
            latency_ms = (time.time() - t) * 1000
            _health.record("chatgpt", latency_ms, True, kind="image")
            _stats.record("chatgpt", req.model, req.prompt, "", latency_ms, True,
                          user_id=uid, is_image_gen=True)
            return {"created": int(t), "data": data}
        except Exception as e:
            latency_ms = (time.time() - t) * 1000
            _health.record("chatgpt", latency_ms, False, str(e)[:120], kind="image")
            _stats.record("chatgpt", req.model, req.prompt, "", latency_ms, False,
                          str(e)[:120], user_id=uid, is_image_gen=True)
            raise HTTPException(502, str(e))

    # Gemini path
    try:
        result = await _gemini_images(req.prompt, [], req.model, req.n, req.response_format)
        latency_ms = (time.time() - t) * 1000
        _health.record("gemini", latency_ms, True, kind="image")
        _stats.record("gemini", req.model, req.prompt, "", latency_ms, True,
                      user_id=uid, is_image_gen=True)
        return result
    except Exception as e:
        latency_ms = (time.time() - t) * 1000
        _health.record("gemini", latency_ms, False, str(e)[:120], kind="image")
        _stats.record("gemini", req.model, req.prompt, "", latency_ms, False,
                      str(e)[:120], user_id=uid, is_image_gen=True)
        raise


@app.post("/v1/images/edits")
async def image_edits(request: Request, user: dict = Depends(_auth.get_current_user)):
    """Image-to-image: multipart form with image file + prompt.

    Accepts:
      image            — primary image (component sheet)
      image_extra_0..N — additional hi-res component crops (optional)
      image_original   — original source image for reference (optional)
      prompt           — recomposition prompt (used as-is, not mutated)
    """
    import base64 as _b64
    form = await request.form()
    prompt = form.get("prompt", "")
    model  = form.get("model") or None
    try:
        n = int(form.get("n", 1))
    except (TypeError, ValueError):
        raise HTTPException(400, "n must be an integer")
    if not (1 <= n <= 4):
        raise HTTPException(400, "n must be between 1 and 4")
    fmt    = form.get("response_format", "b64_json")

    image_field = form.get("image")
    if image_field is None:
        raise HTTPException(400, "image field required")

    all_images: list[bytes] = []
    all_images.append(await image_field.read() if hasattr(image_field, "read") else _b64.b64decode(image_field))

    # Extra hi-res component crops (image_extra_0, image_extra_1, ...)
    i = 0
    while (extra_field := form.get(f"image_extra_{i}")) is not None:
        all_images.append(await extra_field.read() if hasattr(extra_field, "read") else _b64.b64decode(extra_field))
        i += 1

    # Original source image
    if (orig_field := form.get("image_original")) is not None:
        all_images.append(await orig_field.read() if hasattr(orig_field, "read") else _b64.b64decode(orig_field))

    # Enforce faithful/low-temperature behavior via prompt — gemini_webapi has no temperature param.
    # This prefix mimics temperature=0.05: no hallucination, pixel-precise copy of reference images.
    _faithful_prefix = (
        "STRICT MODE: Be 100% faithful to the provided reference images. "
        "Do NOT hallucinate, regenerate, reimagine, or creatively interpret any element. "
        "Copy every visual element EXACTLY as it appears in the reference images — "
        "do not alter colors, fonts, faces, logos, or layout beyond what is explicitly instructed. "
        "Now follow these instructions precisely:\n\n"
    )
    uid = user["id"]
    t = time.time()

    # ChatGPT/DALL-E path: image-to-image editing via an uploaded attachment + prompt.
    if model and model.startswith(("gpt-", "dall-e")):
        try:
            _chatgpt_model_ids = {m["id"] for m in MODELS if m["backend"] == "chatgpt"}
            _underlying_model = model if model in _chatgpt_model_ids else "gpt-4o"
            data = await _chatgpt_images(_faithful_prefix + prompt, _underlying_model, n, files=all_images)
            latency_ms = (time.time() - t) * 1000
            _health.record("chatgpt", latency_ms, True, kind="image")
            _stats.record("chatgpt", model, prompt, "", latency_ms, True,
                          user_id=uid, is_image_gen=True)
            return {"created": int(t), "data": data}
        except Exception as e:
            latency_ms = (time.time() - t) * 1000
            _health.record("chatgpt", latency_ms, False, str(e)[:120], kind="image")
            _stats.record("chatgpt", model, prompt, "", latency_ms, False,
                          str(e)[:120], user_id=uid, is_image_gen=True)
            raise HTTPException(502, str(e))

    # Gemini path (default)
    used_model = model or "gemini-3.8-flash"
    try:
        result = await _gemini_images(_faithful_prefix + prompt, all_images, model, n, fmt)
        latency_ms = (time.time() - t) * 1000
        _health.record("gemini", latency_ms, True, kind="image")
        _stats.record("gemini", used_model, prompt, "", latency_ms, True,
                      user_id=uid, is_image_gen=True)
        return result
    except Exception as e:
        latency_ms = (time.time() - t) * 1000
        _health.record("gemini", latency_ms, False, str(e)[:120], kind="image")
        _stats.record("gemini", used_model, prompt, "", latency_ms, False,
                      str(e)[:120], user_id=uid, is_image_gen=True)
        raise


# ── Stats & Usage ─────────────────────────────────────────────────────────────

@app.get("/stats")
def stats_global():
    """Global stats: total requests, tokens, latency — DB-backed (survives restarts)."""
    return _db.get_global_stats_db(_stats._started_at)


@app.get("/stats/providers")
def stats_providers():
    """Per-provider aggregated stats — DB-backed (survives restarts)."""
    return _db.get_all_providers_stats_db()


@app.get("/stats/providers/{provider}")
def stats_provider(provider: str):
    if provider not in _PROVIDER_INFO:
        raise HTTPException(404, f"Unknown provider '{provider}'")
    all_prov = _db.get_all_providers_stats_db()
    return all_prov.get(provider, {"provider": provider, "total_requests": 0, "total_errors": 0})


@app.get("/stats/models")
def stats_models():
    """Per-model stats sorted by total requests — DB-backed (survives restarts)."""
    return _db.get_all_models_db()


@app.get("/stats/models/{model_id:path}")
def stats_model(model_id: str):
    s = _stats.get_model(model_id)
    if s is None:
        raise HTTPException(404, f"No stats for model '{model_id}' — not used yet")
    return s


@app.get("/requests")
def recent_requests(
    limit: int = 50,
    provider: str = "",
    model: str = "",
    user: dict = Depends(_auth.get_current_user),
):
    """Recent request history — DB-backed (survives restarts). Filter by provider or model. Max 500.
    Admin-only: exposes per-request user_id and error text for all users."""
    if not user.get("is_admin"):
        raise HTTPException(403, "Admin access required")
    limit = min(limit, 500)
    return {
        "requests": _db.get_recent_requests_db(limit, model=model, provider=provider),
        "showing": limit,
    }


@app.get("/dashboard/data")
def dashboard_data(user: dict = Depends(_auth.get_current_user)):
    """JSON data endpoint for the dashboard UI. Admin-only."""
    if not user.get("is_admin"):
        raise HTTPException(403, "Admin access required")
    return {
        "health":    _health.get_all(),
        "stats":     _db.get_global_stats_db(_stats._started_at),
        "providers": _db.get_all_providers_stats_db(),
        "models":    _db.get_all_models_db(),
        "requests":  _db.get_recent_requests_db(20),
    }


@app.get("/chat", response_class=HTMLResponse)
def chat_ui():
    html_path = Path(__file__).parent / "chat.html"
    return HTMLResponse(html_path.read_text())


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    """Serve the web dashboard."""
    html_path = Path(__file__).parent / "dashboard.html"
    return HTMLResponse(html_path.read_text())


# ── Admin endpoints ──────────────────────────────────────────────────────────

class CreateUserRequest(BaseModel):
    name: str
    email: str = ""
    rate_limit_rpm: int = _db.DEFAULT_RATE_LIMIT_RPM  # 0 = unlimited, set explicitly if needed


@app.post("/admin/users")
async def admin_create_user(req: CreateUserRequest, user: dict = Depends(_auth.get_current_user)):
    if not user.get("is_admin"):
        raise HTTPException(403, "Admin access required")
    return _db.create_user(req.name, req.email, req.rate_limit_rpm)


@app.get("/admin/users")
async def admin_list_users(user: dict = Depends(_auth.get_current_user)):
    if not user.get("is_admin"):
        raise HTTPException(403, "Admin access required")
    users = _db.list_users()
    for u in users:
        u["stats"] = _db.get_user_stats(u["id"])
    return users


@app.get("/admin/users/{user_id}")
async def admin_get_user(user_id: str, user: dict = Depends(_auth.get_current_user)):
    if not user.get("is_admin"):
        raise HTTPException(403, "Admin access required")
    all_users = _db.list_users()
    target = next((u for u in all_users if u["id"] == user_id), None)
    if not target:
        raise HTTPException(404, "User not found")
    target["stats"] = _db.get_user_stats(user_id)
    target["recent_requests"] = _db.get_recent_requests_db(20, user_id=user_id)
    return target


@app.delete("/admin/users/{user_id}")
async def admin_deactivate_user(user_id: str, user: dict = Depends(_auth.get_current_user)):
    if not user.get("is_admin"):
        raise HTTPException(403, "Admin access required")
    _db.deactivate_user(user_id)
    return {"status": "deactivated", "user_id": user_id}


@app.get("/admin/db/stats")
async def admin_db_stats(user: dict = Depends(_auth.get_current_user)):
    if not user.get("is_admin"):
        raise HTTPException(403, "Admin access required")
    return {
        "global": _db.get_global_db_stats(),
        "top_models": _db.get_top_models(),
        "usage_by_user": _db.get_usage_by_user(),
    }


# ── Config (session keys) ────────────────────────────────────────────────────

_EDITABLE_KEYS: dict[str, str | None] = {
    "CLAUDE_SESSION_KEY":    "claude",
    "CLAUDE_PROXY_URL":      "claude",
    "GEMINI_1PSID":          "gemini",
    "GEMINI_1PSIDTS":        "gemini",
    "CHATGPT_SESSION_TOKEN": "chatgpt",
    "KIMI_REFRESH_TOKEN":    "kimi",
    "GEMINI_API_KEY":        None,
    "GEMINI_IMAGE_MODEL":    None,
}

_ENV_PATH = Path(__file__).parent.parent / ".env"


def _mask(val: str) -> str:
    if not val:
        return ""
    if len(val) <= 20:
        return val
    return val[:8] + "…" + val[-4:]


def _write_env_key(key: str, value: str) -> None:
    lines = _ENV_PATH.read_text().splitlines() if _ENV_PATH.exists() else []
    found = False
    out = []
    for line in lines:
        if line.startswith(f"{key}="):
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(line)
    if not found:
        out.append(f"{key}={value}")
    _ENV_PATH.write_text("\n".join(out) + "\n")


@app.get("/admin/config")
async def admin_get_config(user: dict = Depends(_auth.get_current_user)):
    if not user.get("is_admin"):
        raise HTTPException(403, "Admin access required")
    return {key: _mask(os.getenv(key, "")) for key in _EDITABLE_KEYS}


class ConfigUpdateRequest(BaseModel):
    key: str
    value: str


@app.put("/admin/config")
async def admin_update_config(req: ConfigUpdateRequest, user: dict = Depends(_auth.get_current_user)):
    if not user.get("is_admin"):
        raise HTTPException(403, "Admin access required")
    if req.key not in _EDITABLE_KEYS:
        raise HTTPException(400, f"Key not editable: {req.key}")

    os.environ[req.key] = req.value
    _write_env_key(req.key, req.value)

    backend = _EDITABLE_KEYS[req.key]
    if not backend:
        return {"status": "updated", "reinit": "none"}

    import importlib
    try:
        # Patch module-level vars captured at import time
        # (chatgpt needs no patch here — init_client() below rebuilds its account
        # pool fresh from os.environ, which was already updated above)
        if backend == "kimi":
            import kimi_proxy.client as _kimi
            _kimi.invalidate()
            if req.key == "KIMI_REFRESH_TOKEN":
                _kimi._device_id = _kimi._decode_jwt(req.value).get("device_id", "") or ""

        m = importlib.import_module(f"{backend}_proxy.client")
        await getattr(m, "init_client")()
    except Exception as e:
        return {"status": "updated", "reinit": "failed", "error": str(e)[:200]}

    return {"status": "updated", "reinit": "ok"}


@app.post("/admin/restart")
async def admin_restart(user: dict = Depends(_auth.get_current_user)):
    if not user.get("is_admin"):
        raise HTTPException(403, "Admin access required")
    import subprocess, threading
    def _do_restart():
        import time
        time.sleep(0.5)
        subprocess.run(["pm2", "restart", "ai-router"], capture_output=True)
    threading.Thread(target=_do_restart, daemon=True).start()
    return {"status": "restarting"}


# ── User self-service ────────────────────────────────────────────────────────

@app.get("/me")
async def me(user: dict = Depends(_auth.get_current_user)):
    result = {k: v for k, v in user.items() if k != "api_key"}
    if user["id"] != "admin":
        result["stats"] = _db.get_user_stats(user["id"])
    return result


@app.get("/me/requests")
async def me_requests(limit: int = 50, user: dict = Depends(_auth.get_current_user)):
    if user["id"] == "admin":
        return {"requests": _db.get_recent_requests_db(min(limit, 500))}
    return {"requests": _db.get_recent_requests_db(min(limit, 500), user_id=user["id"])}


@app.get("/me/stats/models")
async def me_stats_models(user: dict = Depends(_auth.get_current_user)):
    uid = "" if user["id"] == "admin" else user["id"]
    return _db.get_user_model_breakdown(uid)


@app.get("/me/stats/providers")
async def me_stats_providers(user: dict = Depends(_auth.get_current_user)):
    uid = "" if user["id"] == "admin" else user["id"]
    return _db.get_user_provider_breakdown(uid)


@app.get("/me/stats/image-gen")
async def me_stats_image_gen(user: dict = Depends(_auth.get_current_user)):
    uid = "" if user["id"] == "admin" else user["id"]
    return _db.get_image_gen_stats(uid)


@app.get("/admin/users/{user_id}/stats/models")
async def admin_user_stats_models(user_id: str, user: dict = Depends(_auth.get_current_user)):
    if not user.get("is_admin"):
        raise HTTPException(403, "Admin access required")
    return _db.get_user_model_breakdown(user_id)


@app.get("/admin/users/{user_id}/stats/providers")
async def admin_user_stats_providers(user_id: str, user: dict = Depends(_auth.get_current_user)):
    if not user.get("is_admin"):
        raise HTTPException(403, "Admin access required")
    return _db.get_user_provider_breakdown(user_id)


@app.get("/admin/users/{user_id}/stats/image-gen")
async def admin_user_stats_image_gen(user_id: str, user: dict = Depends(_auth.get_current_user)):
    if not user.get("is_admin"):
        raise HTTPException(403, "Admin access required")
    return _db.get_image_gen_stats(user_id)


@app.get("/user", response_class=HTMLResponse)
def user_dashboard():
    """Serve the user dashboard."""
    html_path = Path(__file__).parent / "user_dashboard.html"
    return HTMLResponse(html_path.read_text())


if __name__ == "__main__":
    import socket
    import uvicorn

    def _free_port(start: int = 8000, end: int = 9000) -> int:
        for p in range(start, end):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                if s.connect_ex(("localhost", p)) != 0:
                    return p
        raise RuntimeError("No free port found in range")

    port = int(os.getenv("ROUTER_PORT", "0")) or _free_port()
    print(f"Starting on port {port}")
    uvicorn.run("router.main:app", host="0.0.0.0", port=port)
