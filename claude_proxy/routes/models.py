"""GET /v1/models — known claude.ai web models (Sep 2026)."""
from fastapi import APIRouter

router = APIRouter()

_MODELS = [
    {"id": "claude-haiku-4-5-20251001",  "display_name": "Claude Haiku 4.5"},
    {"id": "claude-sonnet-4-5-20250929", "display_name": "Claude Sonnet 4.5"},
    {"id": "claude-opus-4-5-20251101",   "display_name": "Claude Opus 4.5"},
    {"id": "claude-sonnet-4-6",          "display_name": "Claude Sonnet 4.6"},
    {"id": "claude-opus-4-6",            "display_name": "Claude Opus 4.6"},
    {"id": "claude-sonnet-5",            "display_name": "Claude Sonnet 5"},
    {"id": "claude-opus-5",              "display_name": "Claude Opus 5"},
]

# Aliases accepted by /generate/text and /v1/messages
_ALIASES = {
    "claude-haiku-4-5":  "claude-haiku-4-5-20251001",
    "claude-sonnet-4-5": "claude-sonnet-4-5-20250929",
    "claude-opus-4-5":   "claude-opus-4-5-20251101",
}


@router.get("/v1/models")
def list_models():
    return {
        "object": "list",
        "data": [{"id": m["id"], "object": "model", "display_name": m["display_name"]} for m in _MODELS],
        "aliases": _ALIASES,
    }
