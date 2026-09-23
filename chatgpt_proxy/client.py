"""
ChatGPT proxy client — no HAR file, no browser required.

Two modes (auto-selected based on whether any CHATGPT_SESSION_TOKEN* is set):

  Authenticated (CHATGPT_SESSION_TOKEN set):
    - GET /api/auth/session → 15-min access token (auto-refreshed)
    - POST /backend-api/conversation with Authorization: Bearer
    - Full model selection: gpt-4o, o1, o3-mini, etc.
    - Session cookie from DevTools → Application → Cookies → chatgpt.com
      → __Secure-next-auth.session-token (.0 + .1 concatenated, no space)

  Anonymous (no token):
    - POST /backend-anon/conversation
    - Model locked to "auto" (OpenAI decides, currently GPT-4o)
    - No file upload (image editing/vision input needs an authenticated session)

Both modes use the same PoW challenge flow (SHA3-512, pure Python).
Uses curl_cffi to impersonate Chrome TLS fingerprint (bypasses Cloudflare).
Based on: github.com/gin337/ChatGPTReversed

Multi-account pool (mirrors gemini_proxy/client.py):
  Supports multiple accounts via CHATGPT_SESSION_TOKEN / CHATGPT_SESSION_TOKEN_2 /
  etc. Each account is just a session-token string plus its own cached 15-min
  access token — unlike Gemini there's no persistent live client/cookie jar to
  keep per account, so the pool is a plain list, not a list of client objects.
  rotate() advances to the next account; callers (see router/main.py's
  _chatgpt_images) do this between sequential attempts, never mid-flight, so
  concurrent in-flight requests never race against a pointer change.
"""
import asyncio
import base64
import hashlib
import json
import os
import random
import time
import uuid
from datetime import datetime, timedelta, timezone

from curl_cffi.requests import AsyncSession

BASE = "https://chatgpt.com"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
_IMPERSONATE = "chrome131"


class _Account:
    def __init__(self, session_token: str):
        self.session_token = session_token
        self.access_token = ""
        self.access_expires = 0.0
        # Generated once and reused for every request from this account — a real
        # browser persists one device fingerprint for its whole session; it never
        # regenerates it per message. Randomizing this per-call (the old behavior)
        # makes every request look like a brand-new device logging in, which is a
        # textbook anti-abuse signal when many requests hit the same account in a
        # short window.
        self.device_id = str(uuid.uuid4())


_anon_device_id = str(uuid.uuid4())


_pool: list[_Account] = []
_current_idx: int = 0


def _authenticated() -> bool:
    return bool(_pool)


def _current() -> _Account:
    if not _pool:
        raise RuntimeError("No authenticated ChatGPT account configured")
    return _pool[_current_idx % len(_pool)]


def rotate() -> int:
    """Advance to the next account. Returns new index."""
    global _current_idx
    _current_idx = (_current_idx + 1) % len(_pool)
    print(f"[chatgpt_pool] switched to account {_current_idx + 1}/{len(_pool)}")
    return _current_idx


def pool_size() -> int:
    return len(_pool)


def _headers(accept: str = "application/json", device_id: str = "") -> dict:
    return {
        "accept": accept,
        "content-type": "application/json",
        "cache-control": "no-cache",
        "referer": "https://chatgpt.com/",
        "oai-device-id": device_id,
        "oai-language": "en",
        "user-agent": _UA,
        "sec-ch-ua": '"Not A(Brand";v="8", "Chromium";v="132", "Google Chrome";v="132"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "cors",
    }


def _utc_str(dt: datetime) -> str:
    return dt.strftime("%a, %d %b %Y %H:%M:%S GMT")


def _fake_sentinel_token() -> str:
    now = datetime.now(timezone.utc)
    ts = _utc_str(now).replace("GMT", "GMT+0100 (Central European Time)")
    config = [
        random.randint(3000, 6000), ts, 4294705152, 0, _UA,
        "de", "de", 401, "mediaSession", "location", "scrollX",
        f"{random.uniform(1000, 5000):.4f}", str(uuid.uuid4()), "", 12,
        int(time.time() * 1000),
    ]
    return "gAAAAAC" + base64.b64encode(json.dumps(config, separators=(",", ":")).encode()).decode()


def _solve_pow(seed: str, difficulty: str) -> str:
    core = random.choice([8, 12, 16, 24])
    screen = random.choice([3000, 4000, 6000])
    now = datetime.now(timezone.utc) - timedelta(hours=8)
    parse_time = _utc_str(now).replace("GMT", "GMT+0100 (Central European Time)")
    config = [core + screen, parse_time, 4294705152, 0, _UA]
    diff_len = len(difficulty) // 2

    for i in range(100000):
        config[3] = i
        b64 = base64.b64encode(json.dumps(config, separators=(",", ":")).encode()).decode()
        if hashlib.sha3_512((seed + b64).encode()).hexdigest()[:diff_len] <= difficulty:
            return "gAAAAAB" + b64

    return "gAAAAABwQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D" + base64.b64encode(f'"{seed}"'.encode()).decode()


async def _get_access_token(session: AsyncSession) -> str:
    """Exchange the current pool account's session cookie for a 15-min access
    token. Cached per account."""
    acct = _current()
    if time.monotonic() < acct.access_expires:
        return acct.access_token
    r = await session.get(
        f"{BASE}/api/auth/session",
        headers={
            "user-agent": _UA,
            "cookie": f"__Secure-next-auth.session-token={acct.session_token}",
        },
    )
    data = r.json()
    if not data.get("accessToken"):
        raise RuntimeError("ChatGPT session token expired or invalid — re-export from DevTools")
    acct.access_token = data["accessToken"]
    acct.access_expires = time.monotonic() + 13 * 60
    return acct.access_token


async def _sentinel(session: AsyncSession, device_id: str, csrf: str, access_token: str = "") -> tuple[str, str, str]:
    """Returns (requirements_token, proof_token, oai_sc)."""
    endpoint = "backend-api" if access_token else "backend-anon"
    h = _headers(device_id=device_id)
    h["cookie"] = f"__Host-next-auth.csrf-token={csrf}; oai-did={device_id}; oai-nav-state=1;"
    if access_token:
        h["authorization"] = f"Bearer {access_token}"

    r = await session.post(
        f"{BASE}/{endpoint}/sentinel/chat-requirements",
        json={"p": _fake_sentinel_token()},
        headers=h,
    )
    data = r.json()
    oai_sc = ""
    for cookie_str in r.headers.get("set-cookie", "").split("\n"):
        if "oai-sc=" in cookie_str:
            oai_sc = cookie_str.split("oai-sc=")[1].split(";")[0]
            break

    proof = _solve_pow(data["proofofwork"]["seed"], data["proofofwork"]["difficulty"])
    return data["token"], proof, oai_sc


async def _poll_conversation(session, conv_id: str, access_token: str, full_cookie: str,
                              timeout: float = 90.0) -> str:
    """
    Poll /backend-api/conversation/{conv_id} until assistant response is finished.
    Used for -wm models that return stream_handoff instead of inline SSE.

    Extracts both text AND image_asset_pointer parts (DALL-E tool output can
    land as a "tool"-authored node here too, same as the inline SSE path).
    Previously this only extracted str parts, so an image-only response was
    silently invisible — the loop just kept polling until the timeout fired,
    surfacing as a generic "conduit timeout" with no hint that an image had
    actually been generated. 90s (was 30s) matches the image-gen session
    timeout in ask() — image generation via a -wm model legitimately needs
    that long.
    """
    h = {"user-agent": _UA, "accept": "application/json",
         "authorization": f"Bearer {access_token}", "cookie": full_cookie}
    deadline = time.time() + timeout
    interval = 1.5
    while time.time() < deadline:
        await asyncio.sleep(interval)
        interval = min(interval * 1.2, 3.0)  # back off gently
        r = await session.get(f"{BASE}/backend-api/conversation/{conv_id}", headers=h)
        if r.status_code != 200:
            continue
        mapping = r.json().get("mapping", {})
        text = ""
        image_file_ids: list[str] = []
        for node in mapping.values():
            msg = node.get("message") or {}
            if msg.get("author", {}).get("role") not in ("assistant", "tool"):
                continue
            if msg.get("status") != "finished_successfully":
                continue
            for part in msg.get("content", {}).get("parts", []):
                if isinstance(part, str) and part.strip():
                    text = part.strip()
                elif isinstance(part, dict) and part.get("content_type") == "image_asset_pointer":
                    fid = part.get("asset_pointer", "").split("://", 1)[-1]
                    if fid and fid not in image_file_ids:
                        image_file_ids.append(fid)
        if text or image_file_ids:
            return await _append_images(text, image_file_ids)
    raise RuntimeError(f"ChatGPT conduit timeout — no response after {timeout:.0f}s")


async def _collect(r, session=None, access_token: str = "", full_cookie: str = "",
                    conv_id_box: dict | None = None) -> str:
    """
    Parse ChatGPT SSE stream → final text (+ image markdown if images are generated).
    Handles stream_handoff (used by -wm models) by polling the conversation endpoint.

    conv_id_box: optional mutable dict the caller can read after this returns to
    get the conversation_id (carried at the top level of most chunks throughout
    the stream) — used by ask() to hide the conversation from history afterward.
    """
    result = ""
    image_file_ids: list[str] = []

    def _process_message(msg: dict) -> None:
        """Extract text/image content from an assistant or tool message snapshot.

        Deliberately does NOT signal "turn finished" from this message's own
        status field: internal assistant bubbles (e.g. content_type
        "model_editable_context") also reach status "finished_successfully"
        before the real reply/image arrives, which would cause a premature
        return. The stream's natural end (loop exhaustion) finalizes instead.
        """
        nonlocal result
        if not msg:
            return
        # Skip echoed user/system messages — only assistant replies and tool
        # (e.g. DALL-E) outputs carry content we want.
        if msg.get("author", {}).get("role") not in ("assistant", "tool"):
            return
        content = msg.get("content") or {}
        for part in content.get("parts", []):
            if isinstance(part, str) and part:
                result = part
            elif isinstance(part, dict) and part.get("content_type") == "image_asset_pointer":
                # asset_pointer is a URI like "sediment://file_..." or "file-service://file_..."
                fid = part.get("asset_pointer", "").split("://", 1)[-1]
                if fid and fid not in image_file_ids:
                    image_file_ids.append(fid)

    async for raw in r.aiter_lines():
        line = raw.strip() if isinstance(raw, str) else raw.decode("utf-8", errors="replace").strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(chunk, dict):
            continue

        if conv_id_box is not None and isinstance(chunk.get("conversation_id"), str):
            conv_id_box["id"] = chunk["conversation_id"]

        if chunk.get("error"):
            raise RuntimeError(f"ChatGPT: {chunk['error']} (code: {chunk.get('error_code')})")

        # -wm models: response delivered asynchronously via conduit
        if chunk.get("type") == "stream_handoff":
            if session and access_token:
                return await _poll_conversation(session, chunk["conversation_id"],
                                                access_token, full_cookie)
            return ""  # no session context — can't poll

        # Format A: single patch op
        if chunk.get("o") == "append" and chunk.get("p") == "/message/content/parts/0":
            result += chunk.get("v", "")

        # Format B: array of ops
        elif isinstance(chunk.get("v"), list):
            for op in chunk["v"]:
                if op.get("o") == "append" and op.get("p") == "/message/content/parts/0":
                    result += op.get("v", "")
                if op.get("p") == "/message/status" and op.get("v") == "finished_successfully":
                    return await _append_images(result, image_file_ids)

        # Format C: full message snapshot, wrapped in a patch op (used for
        # tool/DALL-E messages: {"p":"","o":"add","v":{"message": {...}}})
        elif isinstance(chunk.get("v"), dict) and "message" in chunk["v"]:
            _process_message(chunk["v"]["message"])

        # Format D: full message snapshot, unwrapped
        elif "message" in chunk:
            _process_message(chunk["message"])

    return await _append_images(result, image_file_ids)


async def _upload_image(session: AsyncSession, access_token: str, image_bytes: bytes) -> dict:
    """
    Upload one image for use as a message attachment (image-to-image editing,
    or vision input). Three-step flow, verified against the live API:
      1. POST /backend-api/files — register upload, get file_id + a signed
         Azure Blob "upload_url".
      2. PUT the raw bytes to upload_url with x-ms-blob-type: BlockBlob.
      3. POST /backend-api/files/{file_id}/uploaded — mark it processed;
         returns mime_type/file_size_bytes.
    Width/height aren't returned by the API but the model needs them in the
    attachment metadata, so they're read locally via Pillow (already a
    project dependency).
    """
    import io
    from PIL import Image

    with Image.open(io.BytesIO(image_bytes)) as im:
        width, height = im.size
        fmt = (im.format or "PNG").lower()
    ext = "jpg" if fmt in ("jpeg", "jpg") else "png"
    mime = "image/jpeg" if ext == "jpg" else "image/png"
    file_name = f"{uuid.uuid4()}.{ext}"

    h = {"authorization": f"Bearer {access_token}", "user-agent": _UA, "content-type": "application/json"}
    r = await session.post(f"{BASE}/backend-api/files", json={
        "file_name": file_name, "file_size": len(image_bytes), "use_case": "multimodal",
    }, headers=h)
    if r.status_code != 200:
        raise RuntimeError(f"ChatGPT file register {r.status_code}: {r.text[:200]}")
    data = r.json()
    file_id, upload_url = data["file_id"], data["upload_url"]

    r2 = await session.put(upload_url, data=image_bytes, headers={"x-ms-blob-type": "BlockBlob", "content-type": mime})
    if r2.status_code not in (200, 201):
        raise RuntimeError(f"ChatGPT blob upload {r2.status_code}")

    r3 = await session.post(f"{BASE}/backend-api/files/{file_id}/uploaded",
                             json={"file_name": file_name}, headers=h)
    if r3.status_code != 200:
        raise RuntimeError(f"ChatGPT upload confirm {r3.status_code}: {r3.text[:200]}")
    meta = r3.json()

    return {
        "file_id": file_id,
        "size_bytes": meta.get("file_size_bytes", len(image_bytes)),
        "mime_type": meta.get("mime_type", mime),
        "file_name": file_name,
        "width": width,
        "height": height,
    }


async def _hide_conversation(session: AsyncSession, access_token: str, conv_id: str) -> None:
    """
    Hide a conversation from the account's ChatGPT history/sidebar — the same
    PATCH the real web UI sends when you click "Delete chat" (a soft delete
    via the documented API, not an undocumented trick). Used by ask() after
    image generation/editing, which requires keep_history=True because
    ChatGPT refuses to invoke the DALL-E tool in a temporary/incognito chat —
    this removes the resulting visible history entry immediately afterward
    instead. Best-effort: failures are logged, not raised, since the actual
    response was already successfully obtained by the time this runs.
    """
    try:
        h = {"authorization": f"Bearer {access_token}", "user-agent": _UA, "content-type": "application/json"}
        r = await session.patch(f"{BASE}/backend-api/conversation/{conv_id}",
                                 json={"is_visible": False}, headers=h)
        if r.status_code != 200:
            print(f"[chatgpt] failed to hide conversation {conv_id}: {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"[chatgpt] failed to hide conversation {conv_id}: {e}")


_IMAGE_HOST_ALLOWLIST = ("oaiusercontent.com", "chatgpt.com", "openai.com")


def _is_allowed_image_host(url: str) -> bool:
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in _IMAGE_HOST_ALLOWLIST)


async def fetch_image_bytes(url: str) -> bytes:
    """
    Download a generated image from its chatgpt.com "estuary" URL.
    These signed URLs are bearer-token-bound, not just IP-bound — a plain
    GET with no auth or from a session without the Chrome TLS fingerprint
    (impersonate) gets a 403. Needed because the URL in generated markdown
    is unusable by an external caller who doesn't hold our session token.

    `url` is parsed out of model-generated text (see router/main.py's
    _chatgpt_images), so it must never be trusted blindly — sending our
    live Bearer token to an attacker-chosen host would leak the account's
    session. Only chatgpt.com/openai's own image hosts are allowed.
    """
    if not _is_allowed_image_host(url):
        raise RuntimeError(f"Refusing to fetch image from untrusted host: {url[:200]!r}")
    async with AsyncSession(impersonate=_IMPERSONATE) as session:
        access_token = await _get_access_token(session)
        r = await session.get(url, headers={"authorization": f"Bearer {access_token}", "user-agent": _UA})
        if r.status_code != 200:
            raise RuntimeError(f"ChatGPT image download {r.status_code}: {r.text[:200]}")
        return r.content


async def _append_images(text: str, file_ids: list[str]) -> str:
    if not file_ids or not _authenticated():
        return text
    try:
        async with AsyncSession(impersonate=_IMPERSONATE) as session:
            access_token = await _get_access_token(session)
            urls = []
            for fid in file_ids:
                r = await session.get(
                    f"{BASE}/backend-api/files/{fid}/download",
                    headers={"authorization": f"Bearer {access_token}", "user-agent": _UA},
                )
                if r.status_code == 200:
                    data = r.json()
                    url = data.get("download_url") or data.get("url")
                    if url:
                        urls.append(url)
        if urls:
            img_md = "\n".join(f"![generated image]({u})" for u in urls)
            return f"{text}\n\n{img_md}".strip() if text else img_md
    except Exception as e:
        print(f"ChatGPT: failed to fetch image URLs: {e}")
    return text


async def ask(prompt: str, model: str = "gpt-4o", keep_history: bool = False,
              files: list[bytes] | None = None) -> str:
    """
    keep_history: image generation is disabled by ChatGPT in temporary/incognito
    chats, so image requests (generation OR editing) must pass keep_history=True
    to let the DALL-E tool work at all. Despite the name, this does NOT leave
    the conversation visible afterward — once the response is fully collected,
    ask() hides it from the account's ChatGPT history/sidebar via
    _hide_conversation() (the same PATCH the real "Delete chat" button sends).
    Plain text chat keeps the default of False, which never touches real
    history in the first place (history_and_training_disabled stays true).

    files: raw image bytes to attach to this message (image-to-image editing,
    or vision input). Each is uploaded via _upload_image() first. Requires an
    authenticated session — anonymous mode (backend-anon) can't upload files.
    """
    if files and not _authenticated():
        raise RuntimeError("Image upload requires an authenticated ChatGPT session (CHATGPT_SESSION_TOKEN)")

    authenticated = _authenticated()
    device_id = _current().device_id if authenticated else _anon_device_id
    msg_id = str(uuid.uuid4())
    # Image generation/editing can take 20s+; the default 30s session timeout is too tight.
    _timeout = 90 if (keep_history or files) else 30

    async with AsyncSession(impersonate=_IMPERSONATE, timeout=_timeout) as session:
        # CSRF token
        r = await session.get(
            f"{BASE}/api/auth/csrf",
            headers=_headers(device_id=device_id),
        )
        csrf = r.json()["csrfToken"]

        # Access token
        access_token = await _get_access_token(session) if authenticated else ""

        # Sentinel + PoW
        req_token, proof, oai_sc = await _sentinel(session, device_id, csrf, access_token)

        # Conversation
        endpoint = "backend-api" if authenticated else "backend-anon"
        session_token = _current().session_token if authenticated else ""
        full_cookie = (
            f"__Secure-next-auth.session-token={session_token}; "
            f"__Host-next-auth.csrf-token={csrf}; oai-did={device_id}; oai-nav-state=1; oai-sc={oai_sc};"
        )
        h = _headers(accept="text/event-stream", device_id=device_id)
        h["cookie"] = full_cookie
        h["openai-sentinel-chat-requirements-token"] = req_token
        h["openai-sentinel-proof-token"] = proof
        if authenticated:
            h["authorization"] = f"Bearer {access_token}"

        if files:
            attachments = [await _upload_image(session, access_token, f) for f in files]
            content = {
                "content_type": "multimodal_text",
                "parts": [
                    {"content_type": "image_asset_pointer", "asset_pointer": f"file-service://{a['file_id']}",
                     "size_bytes": a["size_bytes"], "width": a["width"], "height": a["height"]}
                    for a in attachments
                ] + [prompt],
            }
            metadata = {"attachments": [
                {"id": a["file_id"], "size": a["size_bytes"], "name": a["file_name"],
                 "mimeType": a["mime_type"], "width": a["width"], "height": a["height"]}
                for a in attachments
            ]}
        else:
            content = {"content_type": "text", "parts": [prompt]}
            metadata = {}

        body = {
            "action": "next",
            "messages": [{
                "id": msg_id,
                "author": {"role": "user"},
                "create_time": time.time(),
                "content": content,
                "metadata": metadata,
            }],
            "parent_message_id": "client-created-root",
            "model": model if authenticated else "auto",
            "history_and_training_disabled": not keep_history,
            "conversation_mode": {"kind": "primary_assistant"},
            "supported_encodings": ["v1"],
            "client_contextual_info": {
                "is_dark_mode": True,
                "time_since_loaded": random.randint(5, 15),
                "page_height": 911, "page_width": 1080,
                "pixel_ratio": 1,
                "screen_height": 1080, "screen_width": 1920,
                "app_name": "chatgpt.com",
            },
        }

        r = await session.post(
            f"{BASE}/{endpoint}/conversation",
            json=body,
            headers=h,
            stream=True,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"ChatGPT error {r.status_code}: {r.text[:200]}")

        conv_id_box: dict = {}
        text = await _collect(r, session=session, access_token=access_token,
                               full_cookie=full_cookie, conv_id_box=conv_id_box)

        if keep_history and authenticated and conv_id_box.get("id"):
            await _hide_conversation(session, access_token, conv_id_box["id"])

        return text


async def init_client():
    global _pool, _current_idx
    _pool = []
    _current_idx = 0

    # Collect all configured accounts: primary + CHATGPT_SESSION_TOKEN_2, _3, ...
    tokens: list[str] = []
    primary = os.environ.get("CHATGPT_SESSION_TOKEN", "").strip()
    if primary:
        tokens.append(primary)
    i = 2
    while True:
        t = os.environ.get(f"CHATGPT_SESSION_TOKEN_{i}", "").strip()
        if t:
            tokens.append(t)
            i += 1
        else:
            break

    if not tokens:
        print("ChatGPT proxy ready — anonymous mode (model: auto / GPT-4o)")
        return

    # Verify + warm the access-token cache for each account up front, in pool
    # order, so a bad token is caught at startup, not mid-request. Mirrors
    # gemini_proxy's init_client(): if a later account fails, the exception
    # propagates (router/main.py logs "chatgpt init failed"), but any
    # accounts already appended stay usable — not wiped on partial failure.
    async with AsyncSession(impersonate=_IMPERSONATE) as session:
        for tok in tokens:
            _pool.append(_Account(tok))
            _current_idx = len(_pool) - 1
            await _get_access_token(session)
    _current_idx = 0
    print(f"ChatGPT proxy ready — authenticated ({len(_pool)} account(s))")


async def close_client():
    pass
