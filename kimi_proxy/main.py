"""
Kimi web proxy — www.kimi.ai (international).
Auth: KIMI_REFRESH_TOKEN from .env (90-day JWT, copy once from localStorage).

Run: uvicorn kimi_proxy.main:app --port 8005 --reload

How to get KIMI_REFRESH_TOKEN (one-time, ~90 days):
  1. Log in to www.kimi.ai in Chrome
  2. DevTools → Console → paste:
       copy(localStorage.getItem('refresh_token'))
  3. Paste value → KIMI_REFRESH_TOKEN in .env
"""
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI

load_dotenv()

from kimi_proxy.client import close_client, init_client
from kimi_proxy.routes.chat import router as chat_router
from kimi_proxy.routes.models import router as models_router
from kimi_proxy.routes.stats import router as stats_router


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_client()
    yield
    await close_client()


app = FastAPI(title="Kimi Proxy", lifespan=lifespan)
app.include_router(chat_router)
app.include_router(models_router)
app.include_router(stats_router)


@app.get("/")
def root():
    return {
        "service": "kimi-proxy",
        "endpoints": ["POST /v1/chat/completions", "POST /generate/text", "GET /models", "GET /stats"],
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("KIMI_PROXY_PORT", "8005"))
    uvicorn.run("kimi_proxy.main:app", host="0.0.0.0", port=port, reload=True)
