"""
Chat endpoints.

Anthropic SDK-compatible:
    POST /v1/messages   ← anthropic Python SDK works unchanged (non-streaming)

Simple endpoint:
    POST /generate/text
    Body: { "prompt": "...", "model": "claude-sonnet-4-6" }

Multi-turn messages (Anthropic format) are flattened into a single prompt string.
"""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from claude_proxy.client import get, snapshot_quota
from claude_proxy.tracker import track

router = APIRouter()

DEFAULT_MODEL = "claude-sonnet-4-6"

# Aliases → full model strings accepted by claude-webapi
MODEL_MAP: dict[str, str] = {
    "claude-haiku-4-5":  "claude-haiku-4-5-20251001",
    "claude-sonnet-4-5": "claude-sonnet-4-5-20250929",
    "claude-opus-4-5":   "claude-opus-4-5-20251101",
    "claude-sonnet":     "claude-sonnet-4-6",
    "claude-opus":       "claude-opus-4-6",
}


def _resolve_model(model: str) -> str:
    return MODEL_MAP.get(model, model)


def _flatten_messages(body: dict) -> str:
    """Anthropic messages array + optional system → single prompt string."""
    parts = []
    system = body.get("system")
    if system:
        text = system if isinstance(system, str) else " ".join(
            b.get("text", "") for b in system if isinstance(b, dict) and b.get("type") == "text"
        )
        if text:
            parts.append(f"System: {text}")

    for msg in body.get("messages", []):
        role = msg.get("role", "user")
        content = msg.get("content", "")
        text = content if isinstance(content, str) else " ".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
        parts.append(f"{'Human' if role == 'user' else 'Assistant'}: {text}")

    return "\n\n".join(parts)


# ── Anthropic SDK-compatible endpoint ─────────────────────────────────────────

@router.post("/v1/messages")
async def messages(request: Request):
    body = await request.json()
    model = _resolve_model(body.get("model", DEFAULT_MODEL))
    prompt = _flatten_messages(body)
    if not prompt.strip():
        raise HTTPException(400, "No message content found in request body")

    async with track("POST /v1/messages", model) as t:
        t.prompt_chars = len(prompt)
        before = snapshot_quota()
        try:
            r = await get().generate_content(prompt, model=model)
        except Exception as e:
            raise HTTPException(500, str(e))
        text = r.text
        t.response_chars = len(text)
        t.set_quota(before, snapshot_quota())

    return {
        "id":      f"msg_proxy_{t.request_id}",
        "type":    "message",
        "role":    "assistant",
        "content": [{"type": "text", "text": text}],
        "model":   model,
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens":  max(1, len(prompt) // 4),
            "output_tokens": max(1, len(text) // 4),
        },
    }


# ── Simple endpoint ───────────────────────────────────────────────────────────

class TextRequest(BaseModel):
    prompt: str
    model: str = DEFAULT_MODEL


@router.post("/generate/text")
async def generate_text(req: TextRequest):
    model = _resolve_model(req.model)
    async with track("POST /generate/text", model) as t:
        t.prompt_chars = len(req.prompt)
        before = snapshot_quota()
        try:
            r = await get().generate_content(req.prompt, model=model)
        except Exception as e:
            raise HTTPException(500, str(e))
        text = r.text
        t.response_chars = len(text)
        t.set_quota(before, snapshot_quota())
    return {"text": text, "model": model}
