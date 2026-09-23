"""Image generation endpoints.

Simple endpoints:
    POST /generate/image
    Body: { "prompt": "...", "model": "gemini-flash", "save_to": "./outputs" }
    Returns: { "images": [{ "b64": "...", "mime": "image/png", "path": "..." }] }

    POST /generate/image/url
    Body: { "prompt": "...", "model": "gemini-flash" }
    Returns: { "urls": ["https://..."] }   ← raw Gemini CDN URLs (short-lived)

NOTE: All 3 models support image generation (flash-lite, flash, pro).
      No separate image model exists — image gen is via generate_content().
"""
import base64
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from gemini_proxy.client import get as get_gemini
from gemini_proxy.tracker import track

router = APIRouter()

DEFAULT_MODEL = "gemini-flash"


def _wants_image(body: dict) -> bool:
    modalities = (
        body.get("generationConfig", {}).get("responseModalities", [])
        or body.get("generation_config", {}).get("response_modalities", [])
    )
    return "IMAGE" in [m.upper() for m in modalities]


async def _download_b64(img) -> tuple[str, str]:
    """Download Image object → (mime_type, base64_string)."""
    with tempfile.TemporaryDirectory() as tmp:
        saved = await img.save(path=tmp)
        data = Path(saved).read_bytes()
        suffix = Path(saved).suffix.lstrip(".") or "png"
        return f"image/{suffix}", base64.b64encode(data).decode()


async def _build_image_response(body: dict, model_name: str):
    """Used by text route to handle SDK requests that want images."""
    from gemini_proxy.routes.text import _extract_prompt

    prompt = _extract_prompt(body)
    if not prompt:
        raise HTTPException(400, "No prompt text in contents")

    async with track(f"POST /v1beta/models/{model_name}:generateContent [image]", model_name) as t:
        t.prompt_chars = len(prompt)
        try:
            r = await get_gemini().generate_content(prompt, model=model_name)
        except Exception as e:
            raise HTTPException(500, str(e))
        t.response_chars = len(r.text or "")
        t.image_count = len(r.images)

    parts = []
    if r.text:
        parts.append({"text": r.text})
    for img in r.images:
        try:
            mime, b64 = await _download_b64(img)
            parts.append({"inlineData": {"mimeType": mime, "data": b64}})
        except Exception as e:
            print(f"[proxy] image download failed: {e}")

    return JSONResponse({
        "candidates": [{
            "content": {"parts": parts, "role": "model"},
            "finishReason": "STOP",
            "index": 0,
        }],
        "modelVersion": model_name,
    })


# ── Simple endpoints ──────────────────────────────────────────────────────────

# save_to is caller-supplied and must never resolve outside this directory —
# otherwise a caller could write generated images to arbitrary paths.
_OUTPUT_ROOT = (Path(__file__).parent.parent / "outputs").resolve()


def _resolve_save_dir(save_to: str | None) -> str:
    if not save_to:
        return tempfile.mkdtemp()
    _OUTPUT_ROOT.mkdir(exist_ok=True)
    candidate = (_OUTPUT_ROOT / save_to).resolve()
    if _OUTPUT_ROOT != candidate and _OUTPUT_ROOT not in candidate.parents:
        raise HTTPException(400, "save_to must be a relative path inside the outputs directory")
    candidate.mkdir(parents=True, exist_ok=True)
    return str(candidate)


class ImageRequest(BaseModel):
    prompt: str
    model: str = DEFAULT_MODEL
    save_to: str | None = None


@router.post("/generate/image")
async def generate_image(req: ImageRequest):
    async with track("POST /generate/image", req.model) as t:
        t.prompt_chars = len(req.prompt)
        try:
            r = await get_gemini().generate_content(req.prompt, model=req.model)
        except Exception as e:
            raise HTTPException(500, str(e))

        if not r.images:
            raise HTTPException(404, "No images returned — try a more visual prompt")

        t.image_count = len(r.images)
        t.response_chars = len(r.text or "")

    save_dir = _resolve_save_dir(req.save_to)
    results = []
    for img in r.images:
        saved = await img.save(path=save_dir)
        data = Path(saved).read_bytes()
        suffix = Path(saved).suffix.lstrip(".") or "png"
        results.append({
            "path": saved,
            "mime": f"image/{suffix}",
            "b64":  base64.b64encode(data).decode(),
        })

    return {"model": req.model, "images": results}


@router.post("/generate/image/url")
async def generate_image_url(req: ImageRequest):
    """Returns raw CDN URLs without downloading. Fast but URLs expire."""
    async with track("POST /generate/image/url", req.model) as t:
        t.prompt_chars = len(req.prompt)
        try:
            r = await get_gemini().generate_content(req.prompt, model=req.model)
        except Exception as e:
            raise HTTPException(500, str(e))

        if not r.images:
            raise HTTPException(404, "No images returned")

        t.image_count = len(r.images)

    return {"model": req.model, "urls": [img.url for img in r.images]}
