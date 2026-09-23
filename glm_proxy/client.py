"""
GLM client — chat.z.ai (Zhipu AI international).

No API key needed. Uses anonymous guest auth + Playwright-based Aliyun
Captcha V3 solver (same approach as g4f but Playwright instead of zendriver,
which conflicts with existing Chrome on macOS).
"""
import hashlib
import hmac
import json
import os
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlencode

import aiohttp

from glm_proxy.captcha import get_captcha_token, invalidate

BASE = "https://chat.z.ai"

_token: str = ""
_user_id: str = ""
_user_name: str = "Guest"
_token_expires: float = 0.0


_SALT_KEY = b"key-@@@@)))()((9))-xxxx&&&%%%%%"


def _signature(request_id: str, ts: str, user_id: str, prompt: str = "") -> str:
    # Real algorithm: time-bucketed HMAC (reverse-engineered from GLM-Free-API)
    # wKey = HMAC-SHA256(SaltKey, floor(ts_ms / 300000))
    # sig  = HMAC-SHA256(wKey, sorted_params | base64(prompt) | ts_ms)
    bucket = str(int(ts) // 300000).encode()
    w_key = hmac.new(_SALT_KEY, bucket, hashlib.sha256).digest()
    sorted_params = ",".join(f"{k}={v}" for k, v in sorted({
        "requestId": request_id,
        "timestamp": ts,
        "user_id": user_id,
    }.items()))
    import base64
    payload = f"{sorted_params}|{base64.b64encode(prompt.encode()).decode()}|{ts}"
    return hmac.new(w_key, payload.encode(), hashlib.sha256).hexdigest()


def _build_url_params(user_id: str) -> dict:
    ts = str(int(time.time() * 1000))
    req_id = str(uuid.uuid4())
    now = datetime.now()
    return {
        "timestamp": ts,
        "requestId": req_id,
        "user_id": user_id,
        "version": "prod-fe-1.1.88",
        "platform": "web",
        "token": _token,
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36",
        "language": "en-US",
        "languages": "en-US,en",
        "timezone": "Africa/Cairo",
        "cookie_enabled": "true",
        "screen_width": "1920",
        "screen_height": "1080",
        "viewport_height": "947",
        "viewport_width": "1920",
        "screen_resolution": "1920x1080",
        "viewport_size": "1920x1080",
        "color_depth": "30",
        "pixel_ratio": "1",
        "current_url": f"{BASE}/",
        "pathname": "/",
        "host": "chat.z.ai",
        "hostname": "chat.z.ai",
        "protocol": "https:",
        "search": "",
        "hash": "",
        "referrer": "",
        "title": "",
        "timezone_offset": "0",
        "local_time": now.strftime("%m/%d/%Y, %I:%M:%S %p"),
        "utc_time": now.astimezone(timezone.utc).isoformat(),
        "is_mobile": "false",
        "is_touch": "false",
        "max_touch_points": "0",
        "browser_name": "chrome",
        "os_name": "mac",
        "signature_timestamp": ts,
    }


def _build_variables() -> dict:
    now = datetime.now()
    p = lambda n: str(n).zfill(2)
    return {
        "{{USER_NAME}}": "User",
        "{{USER_LOCATION}}": "Unknown",
        "{{CURRENT_DATETIME}}": f"{now.year}-{p(now.month)}-{p(now.day)} {p(now.hour)}:{p(now.minute)}:{p(now.second)}",
        "{{CURRENT_DATE}}": f"{now.year}-{p(now.month)}-{p(now.day)}",
        "{{CURRENT_TIME}}": f"{p(now.hour)}:{p(now.minute)}:{p(now.second)}",
        "{{CURRENT_TIMEZONE}}": "Africa/Cairo",
        "{{USER_LANGUAGE}}": "en-US",
    }


async def _ensure_token(session: aiohttp.ClientSession):
    global _token, _user_id, _user_name, _token_expires
    if time.monotonic() < _token_expires:
        return
    session_token = os.environ.get("GLM_SESSION_TOKEN", "").strip()
    if session_token:
        # Signed-in token has no exp — fetch profile once to get user_id/name, cache 24h
        async with session.get(
            f"{BASE}/api/v1/auths/",
            headers={"authorization": f"Bearer {session_token}"},
        ) as r:
            data = await r.json()
        _token = session_token          # use the session token directly for all requests
        _token_expires = time.monotonic() + 24 * 60 * 60
    else:
        # Anonymous guest auth — short-lived token, refresh every 4 min
        async with session.get(f"{BASE}/api/v1/auths/") as r:
            data = await r.json()
        _token = data["token"]
        _token_expires = time.monotonic() + 4 * 60
    _user_id = str(data["id"])
    _user_name = data.get("name", "Guest")


async def _create_chat(session: aiohttp.ClientSession, model_id: str) -> str:
    chat_id = str(uuid.uuid4())
    body = {
        "chat": {
            "id": chat_id,
            "title": "New Chat",
            "models": [model_id],
            "params": {},
            "history": {"messages": {}, "currentId": None},
            "tags": [],
            "flags": [],
            "features": [],
            "mcp_servers": [],
            "enable_thinking": False,
            "reasoning_effort": "",
            "auto_web_search": False,
            "message_version": 1,
            "extra": {},
            "timestamp": int(time.time() * 1000),
            "type": "default",
        }
    }
    async with session.post(
        f"{BASE}/api/v1/chats/new",
        json=body,
        headers={"authorization": f"Bearer {_token}", "content-type": "application/json"},
    ) as r:
        data = await r.json()
    return data.get("id") or data.get("chat", {}).get("id") or chat_id


MODEL_IDS: dict[str, str] = {
    "GLM-5.3-Flash":  "x-preview-l",
    "GLM-5.3":        "glm-5.3",
    "GLM-5.2":        "glm-5.2",
    "GLM-5-Turbo":    "GLM-5-Turbo",
    "GLM-4.7":        "glm-4.7",
    "GLM-4.5":        "0727-360B-API",
    "GLM-4.5-Air":    "0727-106B-API",
    "GLM-4-32B":      "glm-4-air-250414",
    "Z1-32B":         "zero",
    "Z1-Rumination":  "deep-research",
}

DEFAULT_MODEL = "GLM-4.7"


async def _do_ask(session: aiohttp.ClientSession, prompt: str, model_id: str, captcha: str) -> tuple[str, bool]:
    """Returns (response_text, captcha_failed)."""
    url_params = _build_url_params(_user_id)
    req_id = url_params["requestId"]
    ts = url_params["timestamp"]
    sig = _signature(req_id, ts, _user_id, prompt)

    headers = {
        "authorization": f"Bearer {_token}",
        "content-type": "application/json",
        "x-fe-version": "prod-fe-1.1.88",
        "x-region": "overseas",
        "x-signature": sig,
        "origin": BASE,
        "referer": f"{BASE}/",
    }

    chat_id = await _create_chat(session, model_id)

    body = {
        "stream": True,
        "model": model_id,
        "messages": [{"role": "user", "content": prompt}],
        "signature_prompt": prompt[:500],
        "params": {},
        "extra": {},
        "features": {
            "image_generation": False,
            "web_search": False,
            "auto_web_search": False,
            "preview_mode": True,
            "flags": [],
            "vlm_tools_enable": False,
            "enable_thinking": False,
            "reasoning_effort": "",
        },
        "variables": _build_variables(),
        "chat_id": chat_id,
        "id": str(uuid.uuid4()),
        "current_user_message_id": str(uuid.uuid4()),
        "current_user_message_parent_id": None,
        "background_tasks": {"title_generation": False, "tags_generation": False},
        "captcha_verify_param": captcha,
    }

    url = f"{BASE}/api/v2/chat/completions?{urlencode(url_params)}"
    parts: list[str] = []

    async with session.post(url, data=json.dumps(body, separators=(",", ":")), headers=headers) as r:
        if r.status >= 400:
            raise RuntimeError(f"GLM API error {r.status}: {await r.text()}")
        async for raw in r.content:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload:
                continue
            try:
                chunk = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if chunk.get("type") != "chat:completion":
                continue
            data = chunk.get("data", {})
            error = data.get("error")
            if error:
                code = error.get("code", "")
                if "CAPTCHA" in str(code):
                    return "", True   # signal captcha failure → retry
                raise RuntimeError(f"GLM error: {code} — {error.get('detail')}")
            if data.get("phase") == "answer":
                delta = data.get("edit_content") or data.get("delta_content", "")
                if delta:
                    parts.append(delta)

    return "".join(parts), False


_uses_session_token = bool(os.environ.get("GLM_SESSION_TOKEN", "").strip())


async def ask(prompt: str, model: str = DEFAULT_MODEL) -> str:
    model_id = MODEL_IDS.get(model, model)

    async with aiohttp.ClientSession() as session:
        await _ensure_token(session)

        # Try without captcha first if we have a real session token.
        # Authenticated users may not need captcha enforcement.
        if _uses_session_token:
            text, captcha_failed = await _do_ask(session, prompt, model_id, "")
            if not captcha_failed:
                return text
            print("GLM: empty captcha rejected for session token — falling back to Playwright solver")

        for attempt in range(3):
            captcha = await get_captcha_token()
            text, captcha_failed = await _do_ask(session, prompt, model_id, captcha)
            if not captcha_failed:
                return text
            print(f"GLM: captcha rejected (attempt {attempt + 1}), refreshing...")
            invalidate()

    raise RuntimeError("GLM: captcha rejected 3 times — captcha solver may be broken")


async def init_client():
    async with aiohttp.ClientSession() as session:
        await _ensure_token(session)
    mode = "user" if os.environ.get("GLM_SESSION_TOKEN") else "guest"
    print(f"GLM proxy ready — {mode}: {_user_name} ({_user_id})")


async def close_client():
    from glm_proxy.captcha import close_browser
    await close_browser()
