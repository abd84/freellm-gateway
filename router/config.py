"""
Startup environment validation for the AI Router.

Call config.validate() during lifespan to check required env vars
and print a clear startup report.
"""
from __future__ import annotations

import os


_BACKENDS = {
    "claude":  {"env": "CLAUDE_SESSION_KEY",    "label": "Anthropic Claude"},
    "gemini":  {"env": "GEMINI_1PSID",          "label": "Google Gemini"},
    "chatgpt": {"env": "CHATGPT_SESSION_TOKEN", "label": "OpenAI ChatGPT"},
    "kimi":    {"env": "KIMI_REFRESH_TOKEN",    "label": "Moonshot Kimi"},
}


def validate() -> None:
    """Validate env vars and print startup config report."""
    print("\n" + "=" * 50)
    print("  AI Router — Startup Config Check")
    print("=" * 50)

    # Check backends
    configured = []
    missing = []
    for name, info in _BACKENDS.items():
        val = os.getenv(info["env"], "")
        if val:
            configured.append(name)
            print(f"  [OK]   {info['label']:20s}  ({info['env']})")
        else:
            missing.append(name)
            print(f"  [WARN] {info['label']:20s}  ({info['env']} not set)")

    if not configured:
        print("\n  *** ERROR: No backend credentials configured! ***")
        print("  Set at least one of:", ", ".join(b["env"] for b in _BACKENDS.values()))
        raise RuntimeError("No AI backend credentials configured")

    # Admin key check
    admin_key = os.getenv("ADMIN_API_KEY", "")
    if not admin_key:
        print("\n  *** WARNING: ADMIN_API_KEY not set in .env — a random one was generated for this run only (see above). ***")
        print("  *** Set a fixed ADMIN_API_KEY in .env so it doesn't change every restart. ***")
    elif admin_key == "admin":
        print("\n  *** WARNING: ADMIN_API_KEY is set to the guessable default 'admin'! ***")
        print("  *** Set a strong ADMIN_API_KEY before deploying! ***")
    else:
        print(f"\n  [OK]   ADMIN_API_KEY           (set, {len(admin_key)} chars)")

    # CORS
    cors = os.getenv("CORS_ORIGINS", "")
    if cors:
        print(f"  [OK]   CORS_ORIGINS            ({cors})")
    else:
        print(f"  [INFO] CORS_ORIGINS            (not set, same-origin only)")

    print(f"\n  Credentials configured: {len(configured)}/{len(_BACKENDS)}")
    if missing:
        print(f"  Missing credentials: {', '.join(missing)}")
    print("=" * 50 + "\n")
