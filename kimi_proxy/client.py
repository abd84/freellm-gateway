"""
Kimi client — www.kimi.ai (international, Connect/gRPC-JSON API).

Auth: KIMI_REFRESH_TOKEN from .env (90-day JWT, copy once from localStorage).
  Exchanges for a short-lived access_token automatically; zero manual renewal.

  How to get KIMI_REFRESH_TOKEN (one-time, ~90 days):
    1. Log in to www.kimi.ai in Chrome
    2. DevTools → Console → paste:
         copy(localStorage.getItem('refresh_token'))
    3. Paste the value → KIMI_REFRESH_TOKEN in .env

API flow per ask():
  1. GET /api/auth/token/refresh  → access_token (cached 15 min)
  2. POST /api/chat               → chat_id  (new per call)
  3. POST /apiv2/.../Chat         → Connect protocol stream → text

Models (observed):
  k2d6-chat   — K2.6 Instant (default, fast)
  k2-chat     — K2 standard

Uses curl_cffi to impersonate Chrome's TLS fingerprint (same pattern as
claude_proxy/chatgpt_proxy) — kimi.ai sits behind Cloudflare too, and a
plain aiohttp/Python TLS handshake is trivially distinguishable from real
browser traffic via passive JA3 fingerprinting, independent of whether the
auth token and headers are otherwise correct. This was the previous
implementation (plain aiohttp, no impersonation) and is the leading
suspect for why an account got flagged auth.account_abnormal after
sustained automated use — a fresh login resolved that specific flag, but
the underlying non-browser fingerprint was never fixed until now.
"""
import base64
import json
import os
import struct
import threading
import time

from curl_cffi.requests import AsyncSession

BASE = "https://www.kimi.ai"
DEFAULT_MODEL = "k2d6-chat"
_IMPERSONATE = "chrome131"
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"

# ── XID generator (kimi.ai validates IDs against XID format) ─────────────────
_XID_ENC = "0123456789abcdefghijklmnopqrstuv"
_xid_machine = os.urandom(3)
_xid_pid = os.getpid() & 0xFFFF
_xid_ctr = [int.from_bytes(os.urandom(3), "big")]
_xid_lock = threading.Lock()


def _xid() -> str:
    with _xid_lock:
        _xid_ctr[0] = (_xid_ctr[0] + 1) & 0xFFFFFF
        c = _xid_ctr[0]
    t = int(time.time())
    b = bytes([
        (t >> 24) & 0xFF, (t >> 16) & 0xFF, (t >> 8) & 0xFF, t & 0xFF,
        _xid_machine[0], _xid_machine[1], _xid_machine[2],
        (_xid_pid >> 8) & 0xFF, _xid_pid & 0xFF,
        (c >> 16) & 0xFF, (c >> 8) & 0xFF, c & 0xFF,
    ])
    e = _XID_ENC
    return (
        e[b[0] >> 3] + e[((b[0] << 2) | (b[1] >> 6)) & 0x1F] +
        e[(b[1] >> 1) & 0x1F] + e[((b[1] << 4) | (b[2] >> 4)) & 0x1F] +
        e[((b[2] << 1) | (b[3] >> 7)) & 0x1F] + e[(b[3] >> 2) & 0x1F] +
        e[((b[3] << 3) | (b[4] >> 5)) & 0x1F] + e[b[4] & 0x1F] +
        e[b[5] >> 3] + e[((b[5] << 2) | (b[6] >> 6)) & 0x1F] +
        e[(b[6] >> 1) & 0x1F] + e[((b[6] << 4) | (b[7] >> 4)) & 0x1F] +
        e[((b[7] << 1) | (b[8] >> 7)) & 0x1F] + e[(b[8] >> 2) & 0x1F] +
        e[((b[8] << 3) | (b[9] >> 5)) & 0x1F] + e[b[9] & 0x1F] +
        e[b[10] >> 3] + e[((b[10] << 2) | (b[11] >> 6)) & 0x1F] +
        e[(b[11] >> 1) & 0x1F] + e[(b[11] << 4) & 0x1F]
    )


# ── JWT helpers ───────────────────────────────────────────────────────────────

def _decode_jwt(token: str) -> dict:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except Exception:
        return {}


# ── Token cache (access token, 15-min TTL) ───────────────────────────────────
_access_token: str = ""
_expires_at: float = 0.0
# device_id lives in the refresh token (not access token) — extract once
_device_id: str = _decode_jwt(os.environ.get("KIMI_REFRESH_TOKEN", "")).get(
    "device_id", ""
) or ""


def _sec_headers() -> dict:
    """Chrome client-hint / fetch-metadata headers a real browser always sends.
    Not required for the request to succeed, but their absence is one more
    signal distinguishing this from genuine Chrome traffic."""
    return {
        "sec-ch-ua": '"Not A(Brand";v="8", "Chromium";v="132", "Google Chrome";v="132"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
        "accept-language": "en-US,en;q=0.9",
    }


async def _refresh() -> str:
    refresh_token = os.environ["KIMI_REFRESH_TOKEN"]
    async with AsyncSession(impersonate=_IMPERSONATE) as session:
        r = await session.get(
            f"{BASE}/api/auth/token/refresh",
            headers={
                "authorization": f"Bearer {refresh_token}",
                "origin": "https://www.kimi.ai",
                "referer": "https://www.kimi.ai/",
                "user-agent": _UA,
                **_sec_headers(),
            },
        )
        body = r.json()
        if r.status_code >= 400:
            raise RuntimeError(f"Kimi refresh {r.status_code}: {body}")
        token = body.get("access_token")
        if not token:
            raise RuntimeError(f"Kimi refresh: no access_token in {body}")
        return token


async def get_token() -> str:
    global _access_token, _expires_at
    if _access_token and time.time() < _expires_at - 60:
        return _access_token
    _access_token = await _refresh()
    p = _decode_jwt(_access_token)
    _expires_at = float(p.get("exp", time.time() + 900))
    return _access_token


def invalidate():
    global _access_token, _expires_at
    _access_token = ""
    _expires_at = 0.0


# ── Request helpers ───────────────────────────────────────────────────────────

def _frame(body: dict) -> bytes:
    payload = json.dumps(body, separators=(",", ":")).encode()
    return b"\x00" + struct.pack(">I", len(payload)) + payload


def _headers(token: str) -> dict:
    p = _decode_jwt(token)
    return {
        "authorization": f"Bearer {token}",
        "content-type": "application/connect+json",
        "accept": "*/*",
        "origin": "https://www.kimi.ai",
        "referer": "https://www.kimi.ai/",
        "user-agent": _UA,
        "connect-protocol-version": "1",
        "x-language": "en-US",
        "x-msh-platform": "web",
        "x-msh-version": "2.1.0",
        "x-msh-device-id": str(_device_id),
        "x-msh-session-id": str(p.get("ssid", "")),
        "x-traffic-id": str(p.get("sub", "")),
        **_sec_headers(),
    }


async def _create_chat(session: AsyncSession, token: str) -> str:
    """Create a new kimi.ai chat session; returns its ID."""
    p = _decode_jwt(token)
    r = await session.post(
        f"{BASE}/api/chat",
        json={"name": " "},
        headers={
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
            "accept": "application/json",
            "origin": "https://www.kimi.ai",
            "referer": "https://www.kimi.ai/",
            "user-agent": _UA,
            "x-language": "en-US",
            "x-msh-platform": "web",
            "x-msh-version": "2.1.0",
            "x-msh-device-id": str(_device_id),
            "x-msh-session-id": str(p.get("ssid", "")),
            "x-traffic-id": str(p.get("sub", "")),
            **_sec_headers(),
        },
    )
    body = r.json()
    if r.status_code >= 400:
        raise RuntimeError(f"Kimi create chat {r.status_code}: {body}")
    chat_id = body.get("id")
    if not chat_id:
        raise RuntimeError(f"Kimi create chat: no id in {body}")
    return chat_id


# ── Main entry point ──────────────────────────────────────────────────────────

async def ask(prompt: str, model: str = DEFAULT_MODEL) -> str:
    token = await get_token()

    parts: list[str] = []
    buffer = b""
    done = False

    # One shared connection for chat creation + the streaming call, instead of
    # opening a fresh session per HTTP call — closer to how a real browser tab
    # reuses one connection across a page's requests.
    async with AsyncSession(impersonate=_IMPERSONATE) as session:
        chat_id = await _create_chat(session, token)

        body = {
            "chat_id": chat_id,
            "scenario": "SCENARIO_CHAT",
            "tools": [],
            "message": {
                "parent_id": _xid(),
                "role": "user",
                "blocks": [{"message_id": "", "text": {"content": prompt}}],
                "scenario": "SCENARIO_CHAT",
                "is_goal": False,
            },
            "options": {
                "thinking": False,
                "enable_plugin": False,
                "reasoning_effort": "REASONING_EFFORT_LOW",
                "model": model,
            },
            "project_id": "",
        }

        r = await session.post(
            f"{BASE}/apiv2/kimi.gateway.chat.v1.ChatService/Chat",
            data=_frame(body),
            headers=_headers(token),
            stream=True,
        )
        if r.status_code == 401:
            invalidate()
            raise RuntimeError("Kimi: access token rejected — refresh_token may be expired")
        if r.status_code >= 400:
            raise RuntimeError(f"Kimi API error {r.status_code}: {r.text[:300]}")

        async for chunk in r.aiter_content():
            buffer += chunk
            while len(buffer) >= 5:
                length = struct.unpack(">I", buffer[1:5])[0]
                if len(buffer) < 5 + length:
                    break
                flag, frame_bytes = buffer[0], buffer[5: 5 + length]
                buffer = buffer[5 + length:]
                if flag != 0x00:
                    continue
                try:
                    frame = json.loads(frame_bytes)
                except json.JSONDecodeError:
                    continue

                if "done" in frame:
                    done = True
                    break

                mask = frame.get("mask", "")
                if "think" in mask or not mask:
                    continue
                if mask in ("block.text", "block.text.content"):
                    text = frame.get("block", {}).get("text", {}).get("content", "")
                    if text:
                        parts.append(text)
            if done:
                break

    return "".join(parts)


async def init_client():
    token = await get_token()
    p = _decode_jwt(token)
    print(f"Kimi proxy ready — user:{p.get('sub', '?')}")


async def close_client():
    pass
