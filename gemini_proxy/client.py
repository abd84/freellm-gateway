"""
Gemini client pool with account rotation.

Supports multiple accounts via GEMINI_1PSID / GEMINI_1PSID_2 / etc.
When one account's quota is exhausted, rotate() switches to the next.

Cookie auto-refresh:
  gemini-webapi rotates __Secure-1PSIDTS every 10 min via start_auto_refresh().
  We wrap that loop to persist the new value back to .env so restarts stay fresh.

  __Secure-1PSID   — lasts months; re-export only if you sign out of Google
  __Secure-1PSIDTS — auto-rotated; handled automatically
  __Secure-1PSIDCC — optional (GEMINI_1PSIDCC / _2 / ...). GeminiClient's
    constructor doesn't accept it, but a real browser always sends it
    alongside 1PSID/1PSIDTS on every request — a session presenting only a
    partial cookie set (which is all this library sends without this)
    plausibly looks less trustworthy to Google than genuine browser traffic
    and may get invalidated faster. Injected directly into the client's
    internal cookie jar (client._cookies) before init(), since there's no
    public constructor param for it. Experimental — not confirmed to
    actually improve session longevity, only that its absence is a real gap
    versus what a browser sends.

Known reliability gotcha (not fixable in code): __Secure-1PSIDTS is a
rotating token — auto_refresh needs the process to stay running
continuously for refresh_interval (default 600s) before even its first
rotation fires. Frequent restarts during development mean rotation may
never get a chance to run, and the account looks "expired" far sooner than
its displayed cookie expiry would suggest. Using the same Google account in
a real browser at the same time compounds this — whichever side (browser or
this client) rotates 1PSIDTS last invalidates the other's copy.
"""
import asyncio
import enum
import os
import random
import re
import sys
from pathlib import Path

# Python 3.10 compat — gemini-webapi uses StrEnum (3.11+)
if sys.version_info < (3, 11) and not hasattr(enum, "StrEnum"):
    class _StrEnum(str, enum.Enum):
        pass
    enum.StrEnum = _StrEnum  # type: ignore

from gemini_webapi import GeminiClient
from gemini_webapi.utils import rotate_1psidts

_pool: list[GeminiClient] = []
_pool_keys: list[tuple[str, str]] = []  # (psid_key, psidts_key) per slot
_current_idx: int = 0
_env_path = Path(__file__).parent.parent / ".env"


def get() -> GeminiClient:
    if not _pool:
        raise RuntimeError("GeminiClient not initialized yet")
    return _pool[_current_idx % len(_pool)]


def rotate() -> int:
    """Advance to the next account. Returns new index."""
    global _current_idx
    _current_idx = (_current_idx + 1) % len(_pool)
    print(f"[gemini_pool] switched to account {_current_idx + 1}/{len(_pool)}")
    return _current_idx


def pool_size() -> int:
    return len(_pool)


_PSIDTS_PATTERN = re.compile(r"^[A-Za-z0-9_\-./+=]{10,512}$")


def _persist_psidts(client: GeminiClient, psidts_key: str) -> None:
    try:
        new_val = client._live_client.cookies.get("__Secure-1PSIDTS")
        if not new_val or not _env_path.exists():
            return
        if not _PSIDTS_PATTERN.match(new_val):
            print(f"Gemini: rotated {psidts_key} value looks malformed — refusing to write to .env")
            return
        lines = _env_path.read_text().splitlines()
        _env_path.write_text(
            "\n".join(
                f"{psidts_key}={new_val}" if l.startswith(f"{psidts_key}=") else l
                for l in lines
            ) + "\n"
        )
        print(f"Gemini: {psidts_key} rotated and saved to .env")
    except Exception as e:
        print(f"Gemini: failed to persist {psidts_key} to .env: {e}")


def _patch_auto_refresh(client: GeminiClient, psidts_key: str) -> None:
    async def _auto_refresh_with_persist():
        interval = max(client.refresh_interval, 60)
        _is_running = lambda: getattr(client, 'running', None) or getattr(client, '_running', True)
        while _is_running():
            jitter = random.uniform(-15, 15)
            await asyncio.sleep(max(60, interval + jitter))
            if not _is_running():
                break
            try:
                async with client._lock:
                    new = await rotate_1psidts(client._live_client, client.verbose)
                    if new:
                        _persist_psidts(client, psidts_key)
                    else:
                        print(f"Gemini ({psidts_key}): rotation returned no new token — PSID may be invalidated")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"Gemini ({psidts_key}): rotation error — {e}")

    client.start_auto_refresh = _auto_refresh_with_persist


async def init_client():
    global _pool, _pool_keys, _current_idx
    _pool = []
    _pool_keys = []
    _current_idx = 0

    # Collect all configured accounts: primary + GEMINI_1PSID_2, _3, ...
    # GEMINI_1PSIDCC (+ _2, _3, ...) is optional per account.
    account_keys: list[tuple[str, str, str | None]] = []
    if os.getenv("GEMINI_1PSID") and os.getenv("GEMINI_1PSIDTS"):
        account_keys.append(("GEMINI_1PSID", "GEMINI_1PSIDTS", "GEMINI_1PSIDCC"))
    i = 2
    while True:
        pk, tk, ck = f"GEMINI_1PSID_{i}", f"GEMINI_1PSIDTS_{i}", f"GEMINI_1PSIDCC_{i}"
        if os.getenv(pk) and os.getenv(tk):
            account_keys.append((pk, tk, ck))
            i += 1
        else:
            break

    if not account_keys:
        raise RuntimeError("Set GEMINI_1PSID and GEMINI_1PSIDTS in .env")

    for psid_key, psidts_key, psidcc_key in account_keys:
        client = GeminiClient(
            secure_1psid=os.getenv(psid_key),
            secure_1psidts=os.getenv(psidts_key),
        )
        psidcc = os.getenv(psidcc_key) if psidcc_key else None
        if psidcc:
            client._cookies.set("__Secure-1PSIDCC", psidcc, domain=".google.com", secure=True)
            print(f"[gemini_pool] injected {psidcc_key} for {psid_key}")
        _patch_auto_refresh(client, psidts_key)
        await client.init(auto_refresh=True, refresh_interval=600)
        _pool.append(client)
        _pool_keys.append((psid_key, psidts_key))

    print(f"[gemini_pool] initialized {len(_pool)} account(s)")


async def close_client():
    for client in _pool:
        await client.close()
