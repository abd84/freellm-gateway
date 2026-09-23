# freellm-gateway

A self-hosted, OpenAI-compatible API gateway that puts Claude, Gemini, ChatGPT, and Kimi behind **one endpoint** — using your own logged-in browser sessions instead of paid API keys.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## What is this?

Each of these providers gives you a web chat UI for free (or as part of a subscription you already pay for), but no free API. This router reverse-engineers each provider's own web client, authenticates with your session cookies, and exposes the result as a standard `/v1/chat/completions` endpoint — so any tool that speaks the OpenAI API (SDKs, Claude Code, Cursor, your own scripts) can use it.

Everyone who runs this router uses **their own accounts and their own credentials** — nothing here talks to a shared backend or bundles any API keys.

## Features

**API**
- OpenAI-compatible `/v1/chat/completions` (streaming + non-streaming) and Anthropic-compatible `/v1/messages`, so any OpenAI/Anthropic SDK, Claude Code, Cursor, or your own script works against it unmodified
- Image generation (`/v1/images/generations`) and image-to-image editing (`/v1/images/edits`) via Gemini or ChatGPT
- `/v1/models` and `/models` — full model catalog with context length, max output, and capabilities per model
- Automatic fallback-friendly design: every model shares one API surface, so switching providers is a one-line change

**Reliability**
- Multi-account pooling per provider (Gemini, ChatGPT) — configure `_2`, `_3`, ... credentials and the router rotates automatically on quota/error
- Background health checks every 5 minutes plus passive per-request tracking, with separate latency tracking for chat vs. image generation (image gen is inherently much slower — it's never allowed to make a healthy chat backend look broken)
- SQLite-backed stats and request history that survive restarts

**Access control**
- Per-user API keys issued and revoked from the admin panel, each with its own usage stats
- Per-user rate limiting (requests/minute), configurable per key, sane default out of the box
- Every inference endpoint requires a valid key — no anonymous access

**Interfaces**
- **Admin dashboard** (`/dashboard`) — see [below](#admin-dashboard)
- **Web chat UI** (`/chat`) — see [below](#web-chat-ui)

## Supported providers

| Provider | Auth method | Notes |
|---|---|---|
| Claude (Anthropic) | `sessionKey` cookie | |
| Gemini (Google) | `__Secure-1PSID` / `1PSIDTS` cookies | multi-account pooling |
| ChatGPT (OpenAI) | session token | multi-account pooling, image gen/edit |
| Kimi (Moonshot) | `refresh_token` | |

## Quickstart

```bash
git clone https://github.com/<your-fork>/freellm-gateway.git
cd freellm-gateway
bash setup.sh
```

The script installs dependencies, walks you through pasting in your session cookies, and starts the server. Then:

```python
from openai import OpenAI

client = OpenAI(api_key="YOUR_API_KEY", base_url="http://localhost:8001/v1")
response = client.chat.completions.create(
    model="gemini-3-flash",
    messages=[{"role": "user", "content": "Hello!"}],
)
print(response.choices[0].message.content)
```

**Full setup, configuration, deployment, and endpoint reference → [`GUIDE.md`](GUIDE.md).**

## Architecture

```
Client (OpenAI SDK / curl / Claude Code)
        │  POST /v1/chat/completions
        ▼
   FastAPI Router (router/main.py)
        │  routes by model ID prefix
   ┌────┴─────┬──────────┬─────────┐
   │  claude  │  gemini  │ chatgpt │  kimi
   │  proxy   │  proxy   │  proxy  │  proxy
   └────┬─────┴────┬─────┴────┬────┴───┬────┘
        ▼           ▼          ▼        ▼
    claude.ai   gemini.    chatgpt.   kimi.ai
                google.com com
```

Each `*_proxy/client.py` impersonates a real browser session against the provider's own web client — no official API keys anywhere in the stack.

## Web Chat UI

`http://localhost:8001/chat` — a self-contained ChatGPT-style interface for anyone with an API key (admin or user), no separate install:

- **Provider / model switcher** — dropdown covering every model from every configured provider, grouped by provider
- **Multiple conversations** — a sidebar of chats, each kept independently, with rename/delete; history is stored in the browser (`localStorage`), not the server
- **Streaming responses** with a typing indicator, over the same SSE mechanism the API uses
- **Markdown rendering** done via safe DOM construction (headings, code blocks, lists, bold/italic, links) — never `innerHTML` on model output, so nothing the model returns can inject a script into your session
- **Mobile-responsive** — collapsible sidebar on small screens
- Signs in with any API key (admin or a per-user key issued from the dashboard) and remembers it locally until you sign out

## Admin Dashboard

`http://localhost:8001/dashboard` — logs in with `ADMIN_API_KEY`. Everything here is admin-only.

**Overview**
- Top-line stat cards: total requests, tokens, active users, uptime
- A banner that proactively warns when a provider's session cookie looks like it's about to expire or has failed, before it shows up as user-facing errors
- Per-provider health cards (status, latency, error rate, last successful ping) plus charts: request share per provider, top models by usage, success rate per provider, and latency per provider (chat and image generation tracked and charted separately, since image gen is naturally much slower)

**Users panel**
- Create a new user from a name + email — returns a fresh API key shown once, meant to be copied and given to that person immediately
- Table of all users with their status, request count, and token usage
- Click into any user for a detail view: their usage breakdown by model/provider and their recent request history
- Deactivate a user's key on demand (instantly blocks further use, without deleting their history)
- Set a custom rate limit (requests/minute) per user, or unlimited, at creation time

**Session keys panel**
- View (masked) and update each provider's session credentials directly from the browser — no need to SSH in and hand-edit `.env` when a cookie expires, just paste the new value and save
- A restart-server button for when a config change needs a fresh process to take effect

**Recent requests**
- Live table of the most recent requests across all users — model, provider, latency, success/failure, and truncated error text for failed ones

## Legal / disclaimer

This project authenticates using **your own** browser session credentials against each provider's consumer web app, not an official public API. That may be against the terms of service of Claude, Gemini, ChatGPT, and/or Kimi — read each provider's ToS before running this against your account. This code is provided for educational and personal-productivity purposes, with no warranty of any kind (see [LICENSE](LICENSE)). You are responsible for how you use it and for any consequences to your own accounts.

## Contributing

Issues and PRs welcome. Keep changes scoped and tested locally against at least one real provider session before opening a PR — there's no CI that can exercise these backends (they require live, personal credentials).

## License

[MIT](LICENSE)
