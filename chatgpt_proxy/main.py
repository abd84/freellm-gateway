"""
ChatGPT web proxy — mirrors gemini_proxy and claude_proxy.

Reverse-engineers chat.openai.com via re-gpt (Zai-Kun/reverse-engineered-chatgpt).
No official OpenAI API key needed — uses your __Secure-next-auth.session-token cookie.

Run:
    CHATGPT_SESSION_TOKEN=<token> uvicorn chatgpt_proxy.main:app --port 8003

Or set CHATGPT_SESSION_TOKEN in .env, then:
    uvicorn chatgpt_proxy.main:app --port 8003 --reload

Endpoints:
    POST /v1/chat/completions   OpenAI SDK-compatible
    POST /generate/text         Simple { prompt, model } → { text }
    GET  /models                Available models
    GET  /stats                 Request history
    GET  /stats/summary/model   Grouped by model
    GET  /stats/summary/endpoint
    DELETE /stats               Clear history
"""
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI

load_dotenv()

from chatgpt_proxy.client import close_client, init_client
from chatgpt_proxy.routes.chat import router as chat_router
from chatgpt_proxy.routes.models import router as models_router
from chatgpt_proxy.routes.stats import router as stats_router


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_client()
    yield
    await close_client()


app = FastAPI(title="ChatGPT Proxy", lifespan=lifespan)

app.include_router(chat_router)
app.include_router(models_router)
app.include_router(stats_router)


@app.get("/")
def root():
    return {
        "service": "chatgpt-proxy",
        "endpoints": [
            "POST /v1/chat/completions",
            "POST /generate/text",
            "GET  /models",
            "GET  /stats",
        ],
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("CHATGPT_PROXY_PORT", "8003"))
    uvicorn.run("chatgpt_proxy.main:app", host="0.0.0.0", port=port, reload=True)
