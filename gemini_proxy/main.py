"""
Gemini Web API Proxy — standalone local server
Uses browser cookies instead of an API key (Gemini Pro account).

Confirmed models (2026-08-31):
    gemini-flash-lite  → 3.5 Flash-Lite  (fastest, ~48k credits/day)
    gemini-flash       → 3.7 Flash        (all-around, ~48k credits/day)
    gemini-pro         → 3.1 Pro          (best quality, 2400 credits/day)

Endpoints:
    GET  /health
    GET  /v1beta/models                          ← list models
    POST /v1beta/models/{model}:generateContent  ← SDK-compatible (text/image)
    POST /generate/text                          ← simple text
    POST /generate/image                         ← image → base64 + saved file
    POST /generate/image/url                     ← image → CDN URLs (fast, expiring)
    GET  /stats                                  ← recent requests log
    GET  /stats/summary/model                    ← credits + calls grouped by model
    GET  /stats/summary/endpoint                 ← calls grouped by endpoint
    GET  /stats/credits                          ← live quota snapshot
    DELETE /stats                                ← clear history

Setup:
    cp .env.example .env   # fill in your cookies
    python -m gemini_proxy.main
    # or: uvicorn gemini_proxy.main:app --port 8001
"""
import os
from contextlib import asynccontextmanager

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI

load_dotenv()

from gemini_proxy.client import init_client, close_client
from gemini_proxy.routes import images, models, stats, text

PORT = int(os.getenv("PROXY_PORT", 8001))


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_client()
    yield
    await close_client()


app = FastAPI(title="Gemini Proxy", lifespan=lifespan)

app.include_router(models.router)
app.include_router(text.router)
app.include_router(images.router)
app.include_router(stats.router)


@app.get("/health")
def health():
    return {"status": "ok", "proxy": "gemini-webapi"}


if __name__ == "__main__":
    uvicorn.run("gemini_proxy.main:app", host="0.0.0.0", port=PORT, reload=False)
