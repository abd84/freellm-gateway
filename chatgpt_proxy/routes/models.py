"""Static model list."""
from fastapi import APIRouter

router = APIRouter(prefix="/models")

_MODELS = [
    {"id": "gpt-4o",      "object": "model", "owned_by": "openai"},
    {"id": "gpt-4o-mini", "object": "model", "owned_by": "openai"},
    {"id": "o1-mini",     "object": "model", "owned_by": "openai"},
    {"id": "o3-mini",     "object": "model", "owned_by": "openai"},
    # o1 and o3 require higher usage quota — hit usage_limit on current account
]


@router.get("")
def list_models():
    return {"object": "list", "data": _MODELS}
