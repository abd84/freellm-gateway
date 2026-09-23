from fastapi import APIRouter
router = APIRouter(prefix="/models")

_MODELS = [
    {"id": "GLM-5.3-Flash", "object": "model", "owned_by": "zhipuai"},
    {"id": "GLM-5.3",       "object": "model", "owned_by": "zhipuai"},
    {"id": "GLM-5.2",       "object": "model", "owned_by": "zhipuai"},
    {"id": "GLM-5-Turbo",   "object": "model", "owned_by": "zhipuai"},
    {"id": "GLM-4.7",       "object": "model", "owned_by": "zhipuai"},
    {"id": "GLM-4.5",       "object": "model", "owned_by": "zhipuai"},
    {"id": "GLM-4.5-Air",   "object": "model", "owned_by": "zhipuai"},
    {"id": "GLM-4-32B",     "object": "model", "owned_by": "zhipuai"},
    {"id": "Z1-32B",        "object": "model", "owned_by": "zhipuai"},
    {"id": "Z1-Rumination", "object": "model", "owned_by": "zhipuai"},
]

@router.get("")
def list_models():
    return {"object": "list", "data": _MODELS}
