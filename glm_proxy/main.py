"""
GLM web proxy — chat.z.ai (Zhipu AI international).
No official API key — uses a logged-in browser session token (GLM_SESSION_TOKEN).

Run: uvicorn glm_proxy.main:app --port 8004 --reload
"""
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI

load_dotenv()

from glm_proxy.client import close_client, init_client
from glm_proxy.routes.chat import router as chat_router
from glm_proxy.routes.models import router as models_router
from glm_proxy.routes.stats import router as stats_router


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_client()
    yield
    await close_client()


app = FastAPI(title="GLM Proxy", lifespan=lifespan)
app.include_router(chat_router)
app.include_router(models_router)
app.include_router(stats_router)


@app.get("/")
def root():
    return {
        "service": "glm-proxy",
        "note": "Web session auth — set GLM_SESSION_TOKEN in .env",
        "endpoints": ["POST /v1/chat/completions", "POST /generate/text", "GET /models", "GET /stats"],
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("GLM_PROXY_PORT", "8004"))
    uvicorn.run("glm_proxy.main:app", host="0.0.0.0", port=port, reload=True)
