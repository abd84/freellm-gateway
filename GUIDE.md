# freellm-gateway — Complete Guide

Everything you need to install, configure, run, and call this router — one file, start to finish.

- [1. Setup](#1-setup)
- [2. Getting your session credentials](#2-getting-your-session-credentials)
- [3. Configuration](#3-configuration)
- [4. Running it](#4-running-it)
- [5. Deployment](#5-deployment)
- [6. Authentication & users](#6-authentication--users)
- [7. Available models](#7-available-models)
- [8. Calling the API](#8-calling-the-api)
- [9. Image generation & editing](#9-image-generation--editing)
- [10. Connecting other tools](#10-connecting-other-tools)
- [11. API reference](#11-api-reference)
- [12. Monitoring & backups](#12-monitoring--backups)
- [13. Troubleshooting](#13-troubleshooting)

---

## 1. Setup

**Requirements**
- Python 3.10+ (`python3 --version`)
- At least one account with Claude, Gemini, ChatGPT, or Kimi

**Quick install (macOS/Linux)**

```bash
git clone https://github.com/<your-fork>/freellm-gateway.git
cd freellm-gateway
bash setup.sh
```

`setup.sh` creates a virtualenv, installs dependencies, walks you through pasting in your session credentials, and starts the server. That's it for local use — see [Deployment](#5-deployment) for Docker/server setups.

---

## 2. Getting your session credentials

The router authenticates to each provider using **your own logged-in browser session** — not an official API key. `setup.sh` will prompt for these one at a time; here's how to find each one.

### Claude (claude.ai)
1. Open **claude.ai** in Chrome, logged in
2. `F12` → **Application** → **Cookies** → `https://claude.ai`
3. Copy the value of **`sessionKey`** (starts with `sk-ant-si...`)

### Gemini (gemini.google.com)
1. Open **gemini.google.com**, logged in
2. `F12` → **Application** → **Cookies** → `https://gemini.google.com`
3. Copy **`__Secure-1PSID`** and **`__Secure-1PSIDTS`**
4. *(Recommended)* also copy **`__Secure-1PSIDCC`** → `GEMINI_1PSIDCC` in `.env` — a real browser always sends it alongside the other two, so including it makes the session look more like genuine traffic.

> **Multiple accounts:** add `GEMINI_1PSID_2`, `GEMINI_1PSIDTS_2`, `GEMINI_1PSIDCC_2` (repeat for `_3`, etc.) — the router pools them and auto-rotates on quota/error.

### ChatGPT (chatgpt.com)
1. Open **chatgpt.com**, logged in
2. `F12` → **Application** → **Cookies** → `https://chatgpt.com`
3. Copy **`__Secure-next-auth.session-token`**
4. If it's split into `.0`/`.1` (some accounts get a token too long for one cookie), concatenate the values in order with **no separator**.

> **Multiple accounts:** add `CHATGPT_SESSION_TOKEN_2`, `_3`, etc. — same pooling/rotation behavior as Gemini.

### Kimi (kimi.ai)
1. Open **kimi.ai**, logged in
2. `F12` → **Application** → **Cookies** → `https://kimi.ai`
3. Copy **`refresh_token`**

You don't need all four — skip any provider you don't use and press Enter when `setup.sh` asks.

**Cookies expire.** When a provider shows DOWN on the dashboard, repeat the steps above and update `.env`, then `bash start.sh`. Gemini's `1PSIDTS` auto-refreshes in the background, but only once the process has stayed running for several minutes — avoid restarting it repeatedly during testing, or it never gets the chance to refresh.

---

## 3. Configuration

All configuration lives in `.env` (copy `.env.example` to start). Key variables:

| Variable | Required | Description |
|---|---|---|
| `ADMIN_API_KEY` | Strongly recommended | Full admin access (dashboard, user management, config). If unset, a random key is generated **on every restart** and printed to the console — set a fixed one so your dashboard login doesn't change. |
| `CLAUDE_SESSION_KEY` | At least one backend | Anthropic Claude session cookie |
| `GEMINI_1PSID` / `GEMINI_1PSIDTS` / `GEMINI_1PSIDCC` | At least one backend | Google Gemini cookies |
| `CHATGPT_SESSION_TOKEN` | At least one backend | ChatGPT session token |
| `KIMI_REFRESH_TOKEN` | At least one backend | Kimi refresh JWT |
| `CORS_ORIGINS` | No | Comma-separated allowed origins. Empty = same-origin only. Never set to `*` if you need cookies/Authorization to work cross-origin — the router won't send credentialed CORS headers alongside a wildcard origin. |
| `ROUTER_PORT` | No | `0` (default) auto-detects a free port; set a fixed value to force one. |

You need **at least one** provider credential set to start the router.

---

## 4. Running it

```bash
# First time
bash setup.sh

# Every time after
bash start.sh
```

The terminal prints the port it landed on, e.g. `Starting on port 8001`. Then:

- **Dashboard:** `http://localhost:8001/dashboard` — log in with `ADMIN_API_KEY`
- **Chat UI:** `http://localhost:8001/chat`
- **API base:** `http://localhost:8001/v1`

---

## 5. Deployment

### Docker

```bash
cp .env.example .env
# edit .env — set at least one backend + ADMIN_API_KEY
docker compose up -d
curl http://localhost:8000/health
```

### Manual (no Docker)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env
uvicorn router.main:app --host 0.0.0.0 --port 8000
```

Use systemd, supervisor, or pm2 to keep it running in production.

### Nginx + SSL

```bash
sudo apt install nginx
sudo cp nginx.conf /etc/nginx/sites-available/ai-router
sudo ln -s /etc/nginx/sites-available/ai-router /etc/nginx/sites-enabled/
# edit server_name in the config to your domain
sudo nginx -t && sudo systemctl reload nginx

sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d your-domain.com
```

**Before exposing this publicly:** set a strong `ADMIN_API_KEY`, keep `CORS_ORIGINS` scoped to origins you actually trust, and remember every request now requires a valid API key by default (there is no anonymous access) — create keys for anyone who needs one via `/admin/users`.

---

## 6. Authentication & users

Every inference request needs an API key:

```
Authorization: Bearer YOUR_API_KEY
```

`ADMIN_API_KEY` works as a universal key with admin privileges. To issue keys to other people without sharing your admin key:

```bash
# Create a user — response includes their api_key, shown once, save it
curl -X POST http://localhost:8001/admin/users \
  -H "Authorization: Bearer YOUR_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name": "Alice", "email": "alice@example.com"}'

# List users
curl http://localhost:8001/admin/users -H "Authorization: Bearer YOUR_ADMIN_KEY"

# Deactivate a user
curl -X DELETE http://localhost:8001/admin/users/USER_ID -H "Authorization: Bearer YOUR_ADMIN_KEY"
```

New users get a default rate limit of 60 requests/minute; pass `"rate_limit_rpm": 0` in the create-user body for unlimited, or any other number for a custom cap.

---

## 7. Available models

Live list always at `GET /v1/models`. As of this writing:

### Gemini (Google) — free, fast
| Model | Notes |
|-------|-------|
| `gemini-3-flash` | Recommended — fast & reliable |
| `gemini-3-pro` | Most capable |
| `gemini-3-flash-lite` | Fastest & lightest |

### Claude (Anthropic)
| Model | Notes |
|-------|-------|
| `claude-opus-5` | Most capable |
| `claude-sonnet-5` | Fast + smart |
| `claude-opus-4-6` / `claude-sonnet-4-6` / `claude-opus-4-5` | |
| `claude-haiku-4-5` | Fastest |

### ChatGPT (OpenAI)
| Model | Notes |
|-------|-------|
| `gpt-4o` | Reliable, verified for chat + image generation/editing |
| `gpt-5-5` / `gpt-5-6` | |
| `gpt-5.6-sol-wm` / `gpt-6-astra-wm` / `gpt-5.6-terra-wm` / `gpt-5.6-luna-wm` | Named variants |
| `gpt-5-5-thinking` | Extended reasoning |
| `gpt-5-5-mini` / `gpt-5-6-mini` / `gpt-5-3-mini` | Faster, cheaper |
| `research` | Deep Research mode |

> Proxied through a real chatgpt.com session — same account you're logged into in your browser.

### Kimi (Moonshot)
| Model | Notes |
|-------|-------|
| `kimi-latest` / `k2d6-chat` / `k2-chat` / `k2-0711-preview` / `k1.5` | |
| `moonshot-v1-8k` / `moonshot-v1-32k` / `moonshot-v1-128k` | Context-length variants |

---

## 8. Calling the API

The router is OpenAI-compatible — use it anywhere you'd use the OpenAI API.

### curl
```bash
curl http://localhost:8001/v1/chat/completions \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3-flash",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

### Python (OpenAI SDK — recommended)
```python
from openai import OpenAI

client = OpenAI(api_key="YOUR_API_KEY", base_url="http://localhost:8001/v1")

response = client.chat.completions.create(
    model="gemini-3-flash",
    messages=[{"role": "user", "content": "Hello!"}]
)
print(response.choices[0].message.content)
```

### Python (requests)
```python
import requests

r = requests.post(
    "http://localhost:8001/v1/chat/completions",
    headers={"Authorization": "Bearer YOUR_API_KEY"},
    json={"model": "gemini-3-flash", "messages": [{"role": "user", "content": "Hello!"}]},
)
print(r.json()["choices"][0]["message"]["content"])
```

### Python (async / httpx)
```python
import httpx, asyncio

async def ask(prompt: str, model: str = "gemini-3-flash") -> str:
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(
            "http://localhost:8001/v1/chat/completions",
            headers={"Authorization": "Bearer YOUR_API_KEY"},
            json={"model": model, "messages": [{"role": "user", "content": prompt}]},
        )
        return r.json()["choices"][0]["message"]["content"]

print(asyncio.run(ask("Explain quantum computing in one paragraph")))
```

### JavaScript / Node.js (OpenAI SDK)
```javascript
import OpenAI from "openai";

const client = new OpenAI({ apiKey: "YOUR_API_KEY", baseURL: "http://localhost:8001/v1" });

const response = await client.chat.completions.create({
  model: "gemini-3-flash",
  messages: [{ role: "user", content: "Hello!" }],
});
console.log(response.choices[0].message.content);
```

### JavaScript (fetch)
```javascript
const res = await fetch("http://localhost:8001/v1/chat/completions", {
  method: "POST",
  headers: { "Authorization": "Bearer YOUR_API_KEY", "Content-Type": "application/json" },
  body: JSON.stringify({ model: "gemini-3-flash", messages: [{ role: "user", content: "Hello!" }] }),
});
const data = await res.json();
console.log(data.choices[0].message.content);
```

### Dart / Flutter
```dart
import 'dart:convert';
import 'package:http/http.dart' as http;

Future<String> ask(String prompt, {String model = 'gemini-3-flash'}) async {
  final res = await http.post(
    Uri.parse('http://localhost:8001/v1/chat/completions'),
    headers: {'Authorization': 'Bearer YOUR_API_KEY', 'Content-Type': 'application/json'},
    body: jsonEncode({'model': model, 'messages': [{'role': 'user', 'content': prompt}]}),
  );
  return jsonDecode(res.body)['choices'][0]['message']['content'];
}
```

### Swift (iOS / macOS)
```swift
func ask(prompt: String, model: String = "gemini-3-flash") async throws -> String {
    var request = URLRequest(url: URL(string: "http://localhost:8001/v1/chat/completions")!)
    request.httpMethod = "POST"
    request.setValue("Bearer YOUR_API_KEY", forHTTPHeaderField: "Authorization")
    request.setValue("application/json", forHTTPHeaderField: "Content-Type")
    request.httpBody = try JSONSerialization.data(withJSONObject: [
        "model": model,
        "messages": [["role": "user", "content": prompt]]
    ])
    let (data, _) = try await URLSession.shared.data(for: request)
    let json = try JSONSerialization.jsonObject(with: data) as! [String: Any]
    let choices = json["choices"] as! [[String: Any]]
    return (choices[0]["message"] as! [String: Any])["content"] as! String
}
```

### System prompts
```python
response = client.chat.completions.create(
    model="gemini-3-flash",
    messages=[
        {"role": "system", "content": "You are a concise assistant. Reply in under 50 words."},
        {"role": "user", "content": "Explain machine learning"},
    ]
)
```

### Multi-turn chat
Pass the full history each request:
```python
history = []

def chat(message: str, model: str = "gemini-3-flash") -> str:
    history.append({"role": "user", "content": message})
    res = client.chat.completions.create(model=model, messages=history)
    reply = res.choices[0].message.content
    history.append({"role": "assistant", "content": reply})
    return reply
```

### Fallback across models
```python
MODELS = ["gemini-3-flash", "claude-sonnet-4-6", "gpt-4o", "gemini-3-pro"]

def ask_with_fallback(prompt: str) -> str:
    for model in MODELS:
        try:
            res = client.chat.completions.create(
                model=model, messages=[{"role": "user", "content": prompt}], timeout=60,
            )
            return res.choices[0].message.content
        except Exception:
            continue
    raise RuntimeError("All models failed")
```

---

## 9. Image generation & editing

Two backends, picked by the `model` field. Both always return `b64_json` regardless of `response_format` (a generated image's URL is bound to the backend's own session and isn't independently fetchable, so the server downloads it for you).

- **Gemini** — `model` starting with `gemini-*` (or omitted). Prompt directly, e.g. *"a red apple on a wooden table"*.
- **ChatGPT** — `model` set to a real ChatGPT model (`gpt-4o`, etc.) or a placeholder like `dall-e-3` (auto-routed through `gpt-4o`), triggering ChatGPT's DALL-E tool. Supports multiple pooled accounts with automatic rotation on failure.

### Generate

```bash
curl http://localhost:8001/v1/images/generations \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "a sunset over mountains", "model": "gemini-3-flash"}'
```

```python
r = requests.post(
    "http://localhost:8001/v1/images/generations",
    headers={"Authorization": "Bearer YOUR_API_KEY"},
    json={"prompt": "a red apple on a wooden table", "model": "gemini-3-flash"},
)
b64 = r.json()["data"][0]["b64_json"]
open("image.png", "wb").write(base64.b64decode(b64))
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `prompt` | string | required | Image description |
| `model` | string | `gemini-3.8-flash` | Any Gemini model, or any ChatGPT model / `dall-e-3` placeholder |
| `n` | int | `1` | Number of images, 1–4, dispatched in parallel |
| `response_format` | string | `b64_json` | Accepted but always returns `b64_json` |

### Edit (image-to-image)

Multipart form, not JSON. Gemini path applies a strict "don't reimagine, copy exactly" instruction automatically; ChatGPT path uploads the image to the account's file store and attaches it to the edit request.

```bash
curl http://localhost:8001/v1/images/edits \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -F "image=@photo.png" \
  -F "prompt=change the background to a starry night sky" \
  -F "model=gemini-3.8-flash" \
  -F "n=1"
```

```python
r = requests.post(
    "http://localhost:8001/v1/images/edits",
    headers={"Authorization": "Bearer YOUR_API_KEY"},
    files={"image": open("photo.png", "rb")},
    data={"prompt": "change the background to a starry night sky", "model": "gpt-4o"},
)
b64 = r.json()["data"][0]["b64_json"]
open("edited.png", "wb").write(base64.b64decode(b64))
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `image` | file | required | Image to edit |
| `image_extra_0`, `image_extra_1`, ... | file | optional | Additional reference images (Gemini path) |
| `image_original` | file | optional | Original source image for reference (Gemini path) |
| `prompt` | string | required | What to change |
| `model` | string | `gemini-3.8-flash` | Any Gemini model, or any ChatGPT model / `dall-e-3` placeholder |
| `n` | int | `1` | Number of variations, 1–4 |

---

## 10. Connecting other tools

### Claude Code

The router implements the Anthropic `/v1/messages` format:

```bash
claude config set apiBaseUrl http://localhost:8001
claude config set apiKey YOUR_API_KEY
claude config set model gemini-3-flash
```

Revert with `claude config set apiBaseUrl https://api.anthropic.com`. Note: tool use (file edits, bash) requires the real Anthropic API — the router works for chat-based usage in Claude Code.

### Cursor / VS Code AI extensions

- **API Base URL:** `http://localhost:8001/v1`
- **API Key:** `YOUR_API_KEY`
- **Model:** `gemini-3-flash`

---

## 11. API reference

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| `POST` | `/v1/chat/completions` | required | OpenAI-compatible chat |
| `POST` | `/v1/messages` | required | Anthropic-compatible chat (Claude Code) |
| `POST` | `/v1/images/generations` | required | Image generation (Gemini or ChatGPT) |
| `POST` | `/v1/images/edits` | required | Image-to-image editing (Gemini or ChatGPT) |
| `GET`  | `/v1/models` | required | List all models |
| `GET`  | `/health` | none | Provider status & latency |
| `GET`  | `/me` | required | Your account info & usage |
| `GET`  | `/me/stats/models` | required | Your usage by model |
| `GET`  | `/me/stats/providers` | required | Your usage by provider |
| `GET`  | `/me/requests?limit=50` | required | Your recent request history |
| `GET`  | `/chat` | required | Web chat UI |
| `GET`  | `/dashboard` | admin | Admin panel |
| `POST`/`GET`/`DELETE` | `/admin/users` | admin | Create/list/deactivate users |

**Error codes**

| Code | Meaning |
|------|---------|
| `401` | Invalid or missing API key |
| `400` | Unknown model or bad request |
| `403` | Valid key, but not authorized for this endpoint (admin-only) |
| `429` | Rate limited — slow down |
| `502` | Backend provider failed — try a different model |
| `500` | Internal error |

**Tips**
- Default model: `gemini-3-flash` — fastest and most reliable
- Long documents: `moonshot-v1-128k` or `gemini-3-pro`
- Set client timeouts to at least 120s — some backends are slow (see [Troubleshooting](#13-troubleshooting))
- If a model fails, try another — all models share the same API surface

---

## 12. Monitoring & backups

| Endpoint | Description |
|---|---|
| `GET /health` | Quick status of all providers |
| `GET /health/{provider}` | Single provider detail |
| `GET /dashboard` | Web UI dashboard (admin) |
| `GET /stats` | Global usage stats |
| `GET /stats/providers` | Per-provider breakdown |
| `GET /stats/models` | Per-model breakdown |

Access logs: `router/logs/access.log` (rotating, 10 MB max, 5 backups).

All state lives in one SQLite file:
```bash
cp router/router.db router/router.db.backup

# Automated (crontab):
# 0 3 * * * cp /path/to/freellm-gateway/router/router.db /backups/router-$(date +\%Y\%m\%d).db
```

---

## 13. Troubleshooting

**`bash: setup.sh: No such file or directory`** — `cd` into the repo folder first.

**Provider shows DOWN on dashboard** — session cookie expired. Get a fresh one ([§2](#2-getting-your-session-credentials)) and update `.env`, then `bash start.sh`.

**`python3: command not found`** — install Python: `brew install python@3.12` (macOS) or your distro's package manager.

**Port already in use** — the server auto-detects a free port. Set `ROUTER_PORT` in `.env` to force one.

**`ModuleNotFoundError` on startup** — run `bash setup.sh` again; it reinstalls dependencies without touching `.env`.

**Gemini requests are consistently slow (20–90s+)** — this is inherent to the reverse-engineered `gemini-webapi` backend under sustained non-browser traffic, not a bug in this router. Set generous client timeouts for Gemini specifically.

**401 on every request after updating** — `/v1/chat/completions`, `/v1/messages`, and the image endpoints all require a valid API key; there is no anonymous access. Use `ADMIN_API_KEY` or create a user key via `/admin/users`.
