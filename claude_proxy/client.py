"""
Claude proxy — direct curl_cffi implementation.

Replaces claude-webapi with direct HTTP calls using Chrome TLS impersonation.
curl_cffi mimics Chrome's TLS fingerprint, bypassing Cloudflare's bot detection.

Same pattern as chatgpt_proxy/client.py.

Endpoints used:
  GET  /api/organizations                                           → org UUID
  POST /api/organizations/{org}/chat_conversations/{conv}/completion → SSE stream
"""
import json
import os
import re
import time
import uuid
from dataclasses import dataclass

from curl_cffi.requests import AsyncSession

BASE = "https://claude.ai"
_IMPERSONATE = "chrome131"

# Module-level state (initialized by init_client)
_session_key: str = ""
_org_id: str = ""
_device_id: str = str(uuid.uuid4())
_activity_id: str = str(uuid.uuid4())
_proxy: str | None = None


@dataclass
class ModelOutput:
    text: str


def _cookies() -> dict:
    return {
        "sessionKey": _session_key,
        "anthropic-device-id": _device_id,
        "activitySessionId": _activity_id,
        "lastActiveOrg": _org_id,
    }


def _headers(accept: str = "application/json") -> dict:
    return {
        "anthropic-device-id": _device_id,
        "x-activity-session-id": _activity_id,
        "content-type": "application/json",
        "accept": accept,
    }


_PROGRESS_LOG_S = 30  # log streaming progress this often on long responses


async def _parse_sse(r, t0: float, model: str) -> str:
    """Parse Claude's SSE stream, extracting text_delta chunks."""
    full_text = ""
    buf = ""
    first_byte = False
    last_log = t0
    stop_reason = None
    kinds: dict[str, int] = {}  # event kind → count, e.g. "delta:text_delta", "block:tool_use"
    async for chunk in r.aiter_content():
        now = time.time()
        if not first_byte:
            first_byte = True
            print(f"[claude] {model} first byte after {now - t0:.1f}s")
        if now - last_log >= _PROGRESS_LOG_S:
            last_log = now
            print(f"[claude] {model} streaming… {now - t0:.0f}s elapsed, {len(full_text)} chars so far, events={kinds}")
        buf += chunk.decode("utf-8", errors="replace")
        events = re.split(r"\r?\n\r?\n", buf)
        buf = events.pop()
        for event_str in events:
            if not event_str.strip():
                continue
            event_type = data = None
            for line in re.split(r"\r?\n", event_str):
                t = line.strip()
                if t.startswith("event:"):
                    event_type = t[6:].strip()
                elif t.startswith("data:"):
                    data = t[5:].strip()
            if not (event_type and data):
                continue
            try:
                evt = json.loads(data)
            except json.JSONDecodeError:
                continue
            et = evt.get("type")
            if et == "content_block_delta":
                k = f"delta:{evt.get('delta', {}).get('type')}"
            elif et == "content_block_start":
                block = evt.get("content_block", {})
                k = f"block:{block.get('type')}"
                if block.get("type") != "text":
                    print(f"[claude] {model} non-text block started: {block.get('type')} name={block.get('name')}")
            else:
                k = et
            kinds[k] = kinds.get(k, 0) + 1
            if et == "content_block_delta":
                delta = evt.get("delta", {})
                if delta.get("type") == "text_delta":
                    full_text += delta.get("text", "")
            elif evt.get("type") == "message_delta":
                stop_reason = evt.get("delta", {}).get("stop_reason") or stop_reason
            elif evt.get("type") == "error":
                print(f"[claude] {model} upstream error event: {data[:300]}")
            elif evt.get("type") == "message_limit":
                # Quota warning — log but don't raise unless it's a hard limit
                body = evt.get("message_limit", {})
                print(f"[claude] {model} message_limit: {json.dumps(body)[:300]}")
                if body.get("type") == "hit_limit":
                    raise RuntimeError("Claude.ai message limit reached. Try again later.")
    print(f"[claude] {model} done in {time.time() - t0:.1f}s — {len(full_text)} chars, stop_reason={stop_reason}, events={kinds}")
    return full_text


class ClaudeClient:
    async def _upload_file(self, session, conv_id: str, data: bytes,
                           filename: str, content_type: str) -> str:
        from curl_cffi import CurlMime
        url = f"{BASE}/api/organizations/{_org_id}/conversations/{conv_id}/wiggle/upload-file"
        m = CurlMime()
        m.addpart(name="file", data=data, filename=filename, content_type=content_type)
        r = await session.post(
            url, multipart=m, cookies=_cookies(),
            headers={"anthropic-device-id": _device_id,
                     "x-activity-session-id": _activity_id,
                     "accept": "application/json"},
            proxy=_proxy, timeout=60,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"File upload HTTP {r.status_code}: {r.text[:300]}")
        return r.json()["file_uuid"]

    async def generate_content(self, prompt: str, model: str | None = None,
                               files: list[bytes] | None = None) -> ModelOutput:
        import imghdr as _imghdr
        conv_id = str(uuid.uuid4())
        resolved_model = model or "claude-sonnet-4-6"

        async with AsyncSession(impersonate=_IMPERSONATE) as session:
            file_uuids = []
            if files:
                _MIME = {"png": "image/png", "jpeg": "image/jpeg", "jpg": "image/jpeg",
                         "gif": "image/gif", "webp": "image/webp"}
                for i, raw in enumerate(files):
                    ext = _imghdr.what(None, h=raw) or "png"
                    mime = _MIME.get(ext, "image/png")
                    fid = await self._upload_file(session, conv_id, raw, f"img_{i}.{ext}", mime)
                    file_uuids.append(fid)
                    print(f"[claude] uploaded file {i+1}/{len(files)} → {fid[:8]}...")

            payload = {
                "attachments": [],
                "completion_request_id": str(uuid.uuid4()),
                "files": file_uuids,
                "locale": "en-US",
                "model": resolved_model,
                "personalized_styles": [{
                    "isDefault": True, "key": "Default", "name": "Normal",
                    "nameKey": "normal_style_name", "prompt": "Normal\n",
                    "summary": "Default responses from Claude",
                    "summaryKey": "normal_style_summary", "type": "default",
                }],
                "prompt": prompt,
                "rendering_mode": "messages",
                "sync_sources": [],
                "timezone": "UTC",
                # No artifacts tool: claude.ai would put long output into an artifact
                # (tool_use input), which only text_delta parsing would silently drop.
                "tools": [
                    {"name": "web_search", "type": "web_search_v0"},
                ],
                "turn_message_uuids": {
                    "human_message_uuid": str(uuid.uuid4()),
                    "assistant_message_uuid": str(uuid.uuid4()),
                },
                "create_conversation_params": {
                    "name": "",
                    "model": resolved_model,
                    "include_conversation_preferences": True,
                    "is_temporary": True,
                    "enabled_imagine": True,
                },
            }

            url = f"{BASE}/api/organizations/{_org_id}/chat_conversations/{conv_id}/completion"
            t0 = time.time()
            print(f"[claude] {resolved_model} → completion conv={conv_id[:8]} prompt={len(prompt)} chars files={len(file_uuids)}")
            r = await session.post(
                url,
                json=payload,
                cookies=_cookies(),
                headers=_headers(accept="text/event-stream"),
                proxy=_proxy,
                timeout=300,
                stream=True,
            )
            print(f"[claude] {resolved_model} upstream HTTP {r.status_code} after {time.time() - t0:.1f}s")
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
            text = await _parse_sse(r, t0, resolved_model)

        return ModelOutput(text=text)


# ── Singleton ─────────────────────────────────────────────────────────────────

_client: ClaudeClient | None = None


def get() -> ClaudeClient:
    if _client is None:
        raise RuntimeError("ClaudeClient not initialized — call init_client() first")
    return _client


async def init_client() -> None:
    global _client, _session_key, _org_id, _proxy

    _session_key = os.getenv("CLAUDE_SESSION_KEY", "")
    if not _session_key:
        raise RuntimeError("Set CLAUDE_SESSION_KEY in .env")

    _proxy = os.getenv("CLAUDE_PROXY_URL") or None

    async with AsyncSession(impersonate=_IMPERSONATE) as session:
        r = await session.get(
            f"{BASE}/api/organizations",
            cookies={"sessionKey": _session_key, "anthropic-device-id": _device_id},
            headers=_headers(),
            proxy=_proxy,
            timeout=30,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:500]}")

        orgs = r.json()
        if not isinstance(orgs, list) or not orgs:
            raise RuntimeError("No Claude organizations found — is the session key valid?")

        _org_id = orgs[0]["uuid"]

    _client = ClaudeClient()
    proxy_info = f" via {_proxy}" if _proxy else ""
    print(f"Claude proxy ready — org {_org_id[:8]}...{proxy_info}")


async def close_client() -> None:
    global _client
    _client = None
