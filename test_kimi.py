"""Smoke test — Kimi proxy: refresh token exchange + one chat round-trip."""
import asyncio
from dotenv import load_dotenv
load_dotenv()

from kimi_proxy.client import ask, init_client


async def main():
    await init_client()
    reply = await ask("Say 'hello world' and nothing else.")
    print(f"Reply: {reply}")


asyncio.run(main())
