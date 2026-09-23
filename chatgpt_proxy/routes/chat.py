"""
Chat endpoints.

OpenAI SDK-compatible:
    POST /v1/chat/completions  ← openai Python SDK works unchanged (non-streaming)

Simple endpoint:
    POST /generate/text
    Body: { "prompt": "...", "model": "gpt-4o" }

Multi-turn messages (OpenAI format) are flattened into a single prompt string.
"""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from chatgpt_proxy.client import ask
from chatgpt_proxy.tracker import track

router = APIRouter()

DEFAULT_MODEL = "gpt-4o"


def _flatten_messages(body: dict) -> str:
    """OpenAI messages array → single prompt string."""
    parts = []
    for msg in body.get("messages", []):
        role = msg.get("role", "user")
        content = msg.get("content", "")
        text = content if isinstance(content, str) else " ".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
        if role == "system":
            parts.append(f"System: {text}")
        elif role == "user":
            parts.append(f"Human: {text}")
        else:
            parts.append(f"Assistant: {text}")
    return "\n\n".join(parts)


# ── OpenAI SDK-compatible endpoint ───────────────────────────────────────────

@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    model = body.get("model", DEFAULT_MODEL)
    prompt = _flatten_messages(body)
    if not prompt.strip():
        raise HTTPException(400, "No message content found in request body")

    async with track("POST /v1/chat/completions", model) as t:
        t.prompt_chars = len(prompt)
        try:
            text = await ask(prompt, model=model)
        except Exception as e:
            raise HTTPException(500, str(e))
        t.response_chars = len(text)

    return {
        "id":      f"chatcmpl_proxy_{t.request_id}",
        "object":  "chat.completion",
        "model":   model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens":     max(1, len(prompt) // 4),
            "completion_tokens": max(1, len(text) // 4),
            "total_tokens":      max(1, (len(prompt) + len(text)) // 4),
        },
    }


# ── Simple endpoint ───────────────────────────────────────────────────────────

class TextRequest(BaseModel):
    prompt: str
    model: str = DEFAULT_MODEL


@router.post("/generate/text")
async def generate_text(req: TextRequest):
    async with track("POST /generate/text", req.model) as t:
        t.prompt_chars = len(req.prompt)
        try:
            text = await ask(req.prompt, model=req.model)
        except Exception as e:
            raise HTTPException(500, str(e))
        t.response_chars = len(text)
    return {"text": text, "model": req.model}
