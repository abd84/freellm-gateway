from fastapi import APIRouter
router = APIRouter(prefix="/models")

_MODELS = [
    {"id": "k2d6-chat",         "object": "model", "owned_by": "moonshot", "description": "K2.6 Instant — fast, default"},
    {"id": "k2-chat",           "object": "model", "owned_by": "moonshot", "description": "K2 standard"},
    {"id": "kimi-latest",       "object": "model", "owned_by": "moonshot", "description": "Latest Kimi model"},
    {"id": "k2-0711-preview",   "object": "model", "owned_by": "moonshot", "description": "K2 preview (Jul 11)"},
    {"id": "k1.5",              "object": "model", "owned_by": "moonshot", "description": "K1.5"},
    {"id": "moonshot-v1-8k",    "object": "model", "owned_by": "moonshot", "description": "Moonshot v1 — 8k context"},
    {"id": "moonshot-v1-32k",   "object": "model", "owned_by": "moonshot", "description": "Moonshot v1 — 32k context"},
    {"id": "moonshot-v1-128k",  "object": "model", "owned_by": "moonshot", "description": "Moonshot v1 — 128k context"},
]

@router.get("")
def list_models():
    return {"object": "list", "data": _MODELS}
