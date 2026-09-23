"""
Claude Web API Proxy — standalone local server
Uses claude.ai browser sessionKey instead of an Anthropic API key.

Models (claude.ai web, Sep 2026):
    claude-haiku-4-5    — fastest
    claude-sonnet-4-5   — default, best balance
    claude-opus-4-5     — most capable

Endpoints:
    GET  /health
    GET  /v1/models                ← list models
    POST /v1/messages              ← Anthropic SDK-compatible (non-streaming)
    POST /generate/text            ← simple text endpoint
    GET  /stats                    ← recent requests log
    GET  /stats/summary/model      ← calls + timing grouped by model
    GET  /stats/summary/endpoint   ← calls grouped by endpoint
    GET  /stats/credits            ← live usage % (Pro/Max only, may 429)
    DELETE /stats                  ← clear history

Setup:
    cp claude_proxy/.env.example .env   # fill in your sessionKey
    python -m claude_proxy.main
    # or: uvicorn claude_proxy.main:app --port 8002
"""
import os
from contextlib import asynccontextmanager

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI

load_dotenv()

from claude_proxy.client import init_client, close_client
from claude_proxy.routes import chat, models, stats

PORT = int(os.getenv("CLAUDE_PROXY_PORT", 8002))


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_client()
    yield
    await close_client()


app = FastAPI(title="Claude Proxy", lifespan=lifespan)

app.include_router(chat.router)
app.include_router(models.router)
app.include_router(stats.router)


@app.get("/health")
def health():
    return {"status": "ok", "proxy": "claude-webapi"}


if __name__ == "__main__":
    uvicorn.run("claude_proxy.main:app", host="0.0.0.0", port=PORT, reload=False)
