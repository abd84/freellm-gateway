"""
Chat endpoints — GLM proxy.

POST /v1/chat/completions   OpenAI SDK-compatible
POST /generate/text         { "prompt": "...", "model": "GLM-4.7" }
"""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from glm_proxy.client import ask
from glm_proxy.tracker import track

router = APIRouter()

DEFAULT_MODEL = "GLM-4.7"


def _flatten(body: dict) -> str:
    parts = []
    for msg in body.get("messages", []):
        role = msg.get("role", "user")
        content = msg.get("content", "")
        text = content if isinstance(content, str) else " ".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
        prefix = {"system": "System", "user": "Human", "assistant": "Assistant"}.get(role, role.title())
        parts.append(f"{prefix}: {text}")
    return "\n\n".join(parts)


@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    model = body.get("model", DEFAULT_MODEL)
    prompt = _flatten(body)
    if not prompt.strip():
        raise HTTPException(400, "No message content")

    async with track("POST /v1/chat/completions", model) as t:
        t.prompt_chars = len(prompt)
        try:
            text = await ask(prompt, model=model)
        except Exception as e:
            raise HTTPException(500, str(e))
        t.response_chars = len(text)

    return {
        "id": f"chatcmpl_glm_{t.request_id}",
        "object": "chat.completion",
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens":     max(1, len(prompt) // 4),
            "completion_tokens": max(1, len(text) // 4),
            "total_tokens":      max(1, (len(prompt) + len(text)) // 4),
        },
    }


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
