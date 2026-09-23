"""GET /models — list available models for this account."""
from fastapi import APIRouter
from gemini_proxy.client import get as get_gemini

router = APIRouter()

# Models confirmed on a Gemini Pro account (2026-08-31)
# gemini-flash-lite → 3.5 Flash-Lite  (fastest, lowest quota cost)
# gemini-flash      → 3.7 Flash        (all-around)
# gemini-pro        → 3.1 Pro          (advanced reasoning, 2400 credits/day)
# All three support image generation via generate_content()

@router.get("/v1beta/models")
@router.get("/v1/models")
def list_models():
    models = get_gemini().list_models()
    return {
        "models": [
            {
                "name": f"models/{m.model_name}",
                "displayName": m.display_name,
                "description": m.description,
                "isAvailable": m.is_available,
            }
            for m in models
        ]
    }
