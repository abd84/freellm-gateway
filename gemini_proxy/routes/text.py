"""Text generation endpoints.

Gemini API-compatible:
    POST /v1beta/models/{model}:generateContent   ← google-generativeai SDK works unchanged

Simple endpoint:
    POST /generate/text
    Body: { "prompt": "...", "model": "gemini-flash" }
"""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from gemini_proxy.client import get as get_gemini
from gemini_proxy.tracker import track

router = APIRouter()

DEFAULT_MODEL = "gemini-flash"


def _extract_prompt(body: dict) -> str:
    parts = []
    for content in body.get("contents", []):
        for part in content.get("parts", []):
            if "text" in part:
                parts.append(part["text"])
    return "\n".join(parts)


# ── Gemini API-compatible (for google-generativeai SDK) ──────────────────────

@router.post("/v1beta/models/{model_path:path}")
@router.post("/v1/models/{model_path:path}")
async def generate_content_compat(model_path: str, request: Request):
    from gemini_proxy.routes.images import _wants_image, _build_image_response

    model_name = model_path.split(":")[0]
    body = await request.json()

    if _wants_image(body):
        return await _build_image_response(body, model_name)

    prompt = _extract_prompt(body)
    if not prompt:
        raise HTTPException(400, "No prompt text in contents")

    async with track(f"POST /v1beta/models/{model_name}:generateContent", model_name) as t:
        t.prompt_chars = len(prompt)
        try:
            r = await get_gemini().generate_content(prompt, model=model_name)
        except Exception as e:
            raise HTTPException(500, str(e))
        t.response_chars = len(r.text or "")

    return JSONResponse({
        "candidates": [{
            "content": {"parts": [{"text": r.text}], "role": "model"},
            "finishReason": "STOP",
            "index": 0,
        }],
        "modelVersion": model_name,
    })


# ── Simple endpoint ───────────────────────────────────────────────────────────

class TextRequest(BaseModel):
    prompt: str
    model: str = DEFAULT_MODEL


@router.post("/generate/text")
async def generate_text(req: TextRequest):
    async with track("POST /generate/text", req.model) as t:
        t.prompt_chars = len(req.prompt)
        try:
            r = await get_gemini().generate_content(req.prompt, model=req.model)
        except Exception as e:
            raise HTTPException(500, str(e))
        t.response_chars = len(r.text or "")

    return {"text": r.text, "model": req.model}
