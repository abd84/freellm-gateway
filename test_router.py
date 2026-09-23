"""
Extensive router test: reasoning, code, math, long context, concurrency, image gen.
Run: python test_router.py
"""
import asyncio
import importlib
import json
import time
import traceback

from dotenv import load_dotenv
load_dotenv()

from router.client import ask
from router import health as _health

PASS = "✓"
FAIL = "✗"
SEP  = "─" * 70

results: list[dict] = []


def log(backend, test, ok, latency, detail=""):
    icon = PASS if ok else FAIL
    lat  = f"{latency:.1f}s"
    print(f"  {icon} [{backend}] {test:<35} {lat}  {detail[:80]}")
    results.append({"backend": backend, "test": test, "ok": ok, "latency": latency, "detail": detail})


async def run(backend, model, test_name, prompt, check_fn=None):
    t = time.time()
    try:
        text, _ = await ask(prompt, model)
        lat = time.time() - t
        ok = check_fn(text) if check_fn else bool(text.strip())
        detail = text.strip()[:120].replace("\n", " ")
        log(backend, test_name, ok, lat, detail)
        return text
    except Exception as e:
        lat = time.time() - t
        log(backend, test_name, False, lat, f"ERROR: {e}")
        return None


# ── Test suites ───────────────────────────────────────────────────────────────

PROVIDERS = [
    ("claude",  "claude-sonnet-4-6"),
    ("gemini",  "gemini-flash"),
    ("chatgpt", "gpt-4o"),
    ("kimi",    "k2d6-chat"),
]


async def test_basic(backend, model):
    """Simple hello + factual question."""
    await run(backend, model, "basic: greeting",
        "Say exactly two words: hello world",
        lambda t: "hello" in t.lower() and "world" in t.lower())

    await run(backend, model, "basic: factual",
        "What is the capital of France? Reply in one word.",
        lambda t: "paris" in t.lower())


async def test_reasoning(backend, model):
    """Logic puzzle + math."""
    await run(backend, model, "reasoning: logic puzzle",
        "Alice is taller than Bob. Bob is taller than Carol. Who is the shortest? Reply with just the name.",
        lambda t: "carol" in t.lower())

    await run(backend, model, "reasoning: math",
        "What is 17 × 23? Think step by step and give only the final numeric answer.",
        lambda t: "391" in t)

    await run(backend, model, "reasoning: probability",
        "I flip a fair coin 3 times. What is the probability of getting exactly 2 heads? Give the answer as a fraction.",
        lambda t: "3/8" in t or "0.375" in t)


async def test_code(backend, model):
    """Code generation + debugging."""
    await run(backend, model, "code: write function",
        "Write a Python function `fibonacci(n)` that returns the nth Fibonacci number using recursion with memoization. Just the code, no explanation.",
        lambda t: "def fibonacci" in t and ("memo" in t.lower() or "cache" in t.lower() or "lru" in t.lower() or "@" in t))

    await run(backend, model, "code: debug",
        """Find the bug in this Python code and fix it:
def calculate_average(numbers):
    total = 0
    for n in numbers:
        total += n
    return total / len(numbers)
# Bug: crashes on empty list""",
        lambda t: "empty" in t.lower() or "zero" in t.lower() or "len" in t or "if not" in t or "ZeroDivision" in t)

    await run(backend, model, "code: SQL query",
        "Write a SQL query to find the top 5 customers by total order value from tables: orders(id, customer_id, amount) and customers(id, name). Just the SQL.",
        lambda t: "SELECT" in t.upper() and "JOIN" in t.upper() and "LIMIT 5" in t.upper())


async def test_long_context(backend, model):
    """Long prompt processing."""
    long_text = """
    The following is a transcript of a board meeting. Multiple topics were discussed.

    Topic 1 - Q3 Revenue: The CFO reported that Q3 revenue was $4.2 million, up 18% year-over-year.
    The main growth driver was the new enterprise tier launched in July, which brought in 23 new clients.

    Topic 2 - Product Roadmap: The CTO presented 4 planned features for Q4:
    (a) Real-time collaboration, (b) API v2 with GraphQL support, (c) Mobile app redesign,
    (d) AI-powered analytics dashboard. Timeline: all 4 by end of December.

    Topic 3 - Hiring: HR reported 12 open positions. Priority hires: 3 engineers, 2 sales reps, 1 designer.
    Budget approved for $850,000 in new salaries.

    Topic 4 - Churn: Customer success noted churn rate increased from 2.1% to 3.4% in Q3.
    Root cause: onboarding friction. Proposed fix: dedicated onboarding specialist + video tutorials.

    Topic 5 - Partnership: A partnership with DataSync Inc was announced. Revenue share: 70/30 in our favor.
    Expected to generate $600,000 additional ARR in the next 6 months.
    """ * 3  # repeat to make it longer

    await run(backend, model, "long context: extraction",
        long_text + "\n\nQuestion: What was the Q3 revenue, and what was the churn rate increase? Give exact numbers.",
        lambda t: "4.2" in t and ("2.1" in t or "3.4" in t))


async def test_creative(backend, model):
    """Creative writing + summarization."""
    await run(backend, model, "creative: poem",
        "Write a 4-line haiku about artificial intelligence. Strictly follow 5-7-5 syllable structure.",
        lambda t: len(t.strip().split("\n")) >= 3)

    story = await run(backend, model, "creative: story",
        "Write a 3-sentence micro-story about a robot who discovers it has feelings. Be creative.",
        lambda t: len(t.strip()) > 50)

    if story:
        await run(backend, model, "creative: summarize",
            f"Summarize this in exactly 5 words:\n\n{story}",
            lambda t: len(t.strip().split()) <= 8)  # slight buffer


async def test_structured(backend, model):
    """JSON output, lists, structured responses."""
    await run(backend, model, "structured: JSON",
        """Return ONLY valid JSON (no markdown, no explanation) with this structure:
{"name": "John", "age": 30, "skills": ["python", "sql"]}
Replace with fictional data for a senior software engineer named Alice.""",
        lambda t: '"name"' in t and '"skills"' in t)

    await run(backend, model, "structured: list",
        "List exactly 5 programming languages, one per line, numbered 1-5. Nothing else.",
        lambda t: "1." in t and "5." in t)


async def test_multilingual(backend, model):
    """Non-English tasks."""
    await run(backend, model, "multilingual: translate",
        "Translate to Spanish: 'The quick brown fox jumps over the lazy dog'. Just the translation.",
        lambda t: "zorro" in t.lower() or "perro" in t.lower() or "rápido" in t.lower())

    await run(backend, model, "multilingual: detect",
        "What language is this: 'Bonjour, comment allez-vous?' Reply with just the language name.",
        lambda t: "french" in t.lower() or "français" in t.lower())


async def test_concurrency(backend, model):
    """3 simultaneous requests."""
    print(f"  → [{backend}] concurrency: firing 3 simultaneous requests...")
    t = time.time()
    tasks = [
        ask("What is 2+2? Reply with just the number.", model),
        ask("Name the planet closest to the sun. One word.", model),
        ask("What color is the sky? One word.", model),
    ]
    try:
        res = await asyncio.gather(*tasks, return_exceptions=True)
        lat = time.time() - t
        errors = [r for r in res if isinstance(r, Exception)]
        texts  = [r[0] for r in res if not isinstance(r, Exception)]
        ok = len(errors) == 0 and "4" in texts[0] if texts else False
        detail = f"{len(texts)}/3 succeeded" + (f" | errors: {[str(e)[:40] for e in errors]}" if errors else "")
        log(backend, "concurrency: 3 parallel requests", ok, lat, detail)
    except Exception as e:
        log(backend, "concurrency: 3 parallel requests", False, time.time()-t, str(e))


async def test_image_gen(backend, model):
    """Image generation — Gemini only."""
    if backend != "gemini":
        print(f"  ↷ [{backend}] image gen: skipped (not supported by this backend)")
        return

    from gemini_proxy.client import get as get_gemini
    print(f"  → [{backend}] image gen: generating image...")
    t = time.time()
    try:
        r = await get_gemini().generate_content(
            "Generate a photorealistic image of a glowing neon city skyline at night, cyberpunk style.",
            model=model
        )
        lat = time.time() - t
        images = r.images if hasattr(r, "images") else []
        ok = len(images) > 0
        detail = f"{len(images)} image(s) returned" if ok else "no images in response"
        log(backend, "image gen: cyberpunk city", ok, lat, detail)
    except Exception as e:
        log(backend, "image gen: cyberpunk city", False, time.time()-t, str(e))


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    # Init all backends
    print("\nInitializing backends...")
    for mod, fn in [
        ("claude_proxy.client",  "init_client"),
        ("gemini_proxy.client",  "init_client"),
        ("chatgpt_proxy.client", "init_client"),
        ("kimi_proxy.client",    "init_client"),
    ]:
        try:
            await getattr(importlib.import_module(mod), fn)()
        except Exception as e:
            print(f"  ✗ {mod}: {e}")

    for backend, model in PROVIDERS:
        print(f"\n{SEP}")
        print(f"  PROVIDER: {backend.upper()}  (model: {model})")
        print(SEP)

        await test_basic(backend, model)
        await test_reasoning(backend, model)
        await test_code(backend, model)
        await test_long_context(backend, model)
        await test_creative(backend, model)
        await test_structured(backend, model)
        await test_multilingual(backend, model)
        await test_concurrency(backend, model)
        await test_image_gen(backend, model)

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{SEP}")
    print("  SUMMARY")
    print(SEP)

    by_backend: dict[str, list] = {}
    for r in results:
        by_backend.setdefault(r["backend"], []).append(r)

    total_pass = total_fail = 0
    for backend, tests in by_backend.items():
        passed = sum(1 for t in tests if t["ok"])
        failed = len(tests) - passed
        avg_lat = sum(t["latency"] for t in tests) / len(tests)
        total_pass += passed
        total_fail += failed
        print(f"  {backend:<10} {passed}/{len(tests)} passed   avg latency: {avg_lat:.1f}s")

    print(f"\n  TOTAL: {total_pass}/{total_pass+total_fail} passed  "
          f"({100*total_pass//(total_pass+total_fail)}%)")

    # Failed tests
    failed = [r for r in results if not r["ok"]]
    if failed:
        print(f"\n  FAILED TESTS:")
        for r in failed:
            print(f"    ✗ [{r['backend']}] {r['test']}")
            if r["detail"]:
                print(f"         {r['detail'][:100]}")


asyncio.run(main())
