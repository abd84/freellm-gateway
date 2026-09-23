"""
Aliyun Captcha V3 solver — persistent browser, one page per solve.

Keeps a single Playwright browser alive for the proxy lifetime.
Each solve opens a new page (not a new browser) — avoids the repeated
cold-start fingerprint that causes Aliyun F001 detection failures.
"""
import asyncio
import os
from pathlib import Path

_SDK_PATH = Path(os.getenv(
    "GLM_CAPTCHA_SDK_PATH",
    "/opt/anaconda3/lib/python3.11/site-packages/g4f/Provider/glm/AliyunCaptcha.js.txt",
))
_CAPTCHA_CONFIG = {"region": "sgp", "prefix": "no8xfe", "sceneId": "didk33e0"}

# ── Persistent browser state ──────────────────────────────────────────────────
_pw = None          # playwright instance
_browser = None     # single long-lived browser
_browser_lock: asyncio.Lock | None = None

# ── Token pool ────────────────────────────────────────────────────────────────
_cache: dict = {"token": None}      # single-use, no TTL
_prefetch_task: asyncio.Task | None = None
_solve_lock: asyncio.Lock | None = None


def _get_lock(name: str) -> asyncio.Lock:
    global _browser_lock, _solve_lock
    if name == "browser":
        if _browser_lock is None:
            _browser_lock = asyncio.Lock()
        return _browser_lock
    if _solve_lock is None:
        _solve_lock = asyncio.Lock()
    return _solve_lock


def _build_html() -> str:
    if not _SDK_PATH.exists():
        raise FileNotFoundError(
            f"GLM captcha SDK not found at {_SDK_PATH}. "
            "Set GLM_CAPTCHA_SDK_PATH in .env to its location (bundled with g4f's glm provider)."
        )
    sdk = _SDK_PATH.read_text(encoding="utf-8")
    safe_sdk = sdk.replace("</script>", "<\\/script>")
    return f"""<!DOCTYPE html><html><head></head><body>
<div id="captcha-element"></div>
<button id="captcha-button"></button>
<script>{safe_sdk}</script>
</body></html>"""


_STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
delete navigator.__proto__.webdriver;
window.chrome = {
    runtime: { id: undefined, connect: () => {}, sendMessage: () => {} },
    loadTimes: () => ({ firstPaintAfterLoadTime: 0.5, requestTime: Date.now() / 1000 }),
    csi: () => ({ startE: Date.now(), onloadT: Date.now(), pageT: 1000, tran: 15 }),
    app: { isInstalled: false, getDetails: () => null, getIsInstalled: () => false },
};
Object.defineProperty(navigator, 'plugins', { get: () => {
    const arr = [
        { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
        { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
        { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' },
    ];
    arr.item = (i) => arr[i]; arr.namedItem = (n) => arr.find(p => p.name === n); arr.refresh = () => {};
    return arr;
}});
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
Object.defineProperty(navigator, 'maxTouchPoints', { get: () => 0 });
Object.defineProperty(navigator, 'vendor', { get: () => 'Google Inc.' });
Object.defineProperty(navigator, 'platform', { get: () => 'MacIntel' });
const origQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) =>
    parameters.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : origQuery(parameters);
"""

_SOLVE_JS = """
async (cfg) => {
    return new Promise((resolve, reject) => {
        const timeout = setTimeout(
            () => reject(new Error('Captcha solve timeout after ' + cfg.timeout + 'ms')),
            cfg.timeout
        );
        window.initAliyunCaptcha({
            SceneId: cfg.sceneId,
            mode: 'popup',
            region: cfg.region,
            prefix: cfg.prefix,
            language: 'en',
            element: '#captcha-element',
            button: '#captcha-button',
            captchaLogoImg: '',
            showErrorTip: false,
            success: (param) => { clearTimeout(timeout); resolve(param); },
            fail: (err) => { clearTimeout(timeout); reject(new Error('SDK fail: ' + JSON.stringify(err))); },
            getInstance: (inst) => { inst.startTracelessVerification(); }
        });
    });
}
"""


async def _ensure_browser():
    """Return the persistent browser, launching it if not yet alive."""
    global _pw, _browser
    if _browser and _browser.is_connected():
        return _browser
    async with _get_lock("browser"):
        if _browser and _browser.is_connected():
            return _browser
        from playwright.async_api import async_playwright
        if _pw is None:
            _pw = await async_playwright().start()
        try:
            _browser = await _pw.chromium.launch(
                channel="chrome",
                headless=True,
                args=["--disable-blink-features=AutomationControlled", "--no-first-run"],
            )
        except Exception:
            _browser = await _pw.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
        return _browser


async def close_browser():
    """Call on proxy shutdown to clean up Playwright resources."""
    global _pw, _browser
    if _browser:
        try:
            await _browser.close()
        except Exception:
            pass
        _browser = None
    if _pw:
        try:
            await _pw.stop()
        except Exception:
            pass
        _pw = None


async def _solve_once() -> str:
    """Open a new page in the persistent browser, solve captcha, close page."""
    browser = await _ensure_browser()
    html = _build_html()
    cfg = _CAPTCHA_CONFIG

    context = await browser.new_context(
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36",
        viewport={"width": 1920, "height": 1080},
        locale="en-US",
    )
    await context.add_init_script(_STEALTH_SCRIPT)
    page = await context.new_page()

    try:
        async def route_handler(route):
            if "chat.z.ai" in route.request.url and route.request.resource_type == "document":
                await route.fulfill(status=200, content_type="text/html", body=html)
            else:
                await route.continue_()

        await page.route("**/*", route_handler)
        await page.goto("https://chat.z.ai/", wait_until="domcontentloaded")
        await page.wait_for_function("typeof window.initAliyunCaptcha === 'function'", timeout=20000)

        token = await page.evaluate(
            _SOLVE_JS,
            {"region": cfg["region"], "prefix": cfg["prefix"], "sceneId": cfg["sceneId"], "timeout": 40000},
        )
    finally:
        await page.unroute_all(behavior="ignoreErrors")  # suppress TargetClosedError warnings
        await context.close()

    if not isinstance(token, str) or not token:
        raise RuntimeError(f"Captcha solver returned invalid token: {token!r}")
    return token


async def _prefetch() -> None:
    """Solve and store the next token in the background. Retries once on F001."""
    async with _get_lock("solve"):
        if _cache["token"]:
            return
        print("GLM: pre-solving captcha in background...")
        for attempt in range(2):
            try:
                token = await _solve_once()
                _cache["token"] = token
                print(f"GLM: captcha pre-solved (token: {token[:30]}...)")
                return
            except Exception as e:
                if attempt == 0:
                    print(f"GLM: pre-solve attempt 1 failed ({e!s:.60}), retrying...")
                else:
                    print(f"GLM: background captcha solve failed after 2 attempts")


async def get_captcha_token() -> str:
    """Consume the pre-solved token (single-use). Kicks off next solve in background."""
    global _prefetch_task

    if not _cache["token"]:
        async with _get_lock("solve"):
            if not _cache["token"]:
                print("GLM: solving Aliyun captcha...")
                for attempt in range(3):
                    try:
                        token = await _solve_once()
                        _cache["token"] = token
                        print(f"GLM: captcha solved (token: {token[:30]}...)")
                        break
                    except Exception as e:
                        if attempt < 2:
                            print(f"GLM: solve attempt {attempt+1} failed, retrying...")
                        else:
                            raise RuntimeError(f"Captcha solve failed after 3 attempts: {e}") from e

    token = _cache["token"]
    _cache["token"] = None

    if _prefetch_task is None or _prefetch_task.done():
        _prefetch_task = asyncio.create_task(_prefetch())

    return token


def invalidate() -> None:
    _cache["token"] = None
