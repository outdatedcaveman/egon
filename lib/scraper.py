"""Shared Playwright helpers for login-based adapters.

Why this module exists
----------------------
Several adapters can't authenticate via a public API (Kindle, Paperpile,
TVTime, Letterboxd, etc.). They need a real browser session — we drive
Playwright once interactively for the user to sign in, save the cookie
jar, then reuse it headless on every subsequent sync.

The 2026-05-20 rewrite (vs. previous version)
---------------------------------------------
The old implementation used `browser.new_context()` with a fresh, sterile
fingerprint each run, so services with anti-bot defences (reCAPTCHA
Enterprise on Paperpile, Cloudflare bot scoring on Amazon, etc.) blocked
the login window before the user even saw the form.

This version adopts three measures that together make the launched window
look like a normal human-driven Chrome to the remote site:

  1. `launch_persistent_context(user_data_dir=...)` — gives each adapter
     its own permanent Chrome profile on disk (`state/browser/<name>/profile/`).
     Cookies, localStorage, IndexedDB, even the password manager — all
     persist across runs. Sites see a "lived-in" browser.
  2. `--disable-blink-features=AutomationControlled` — removes the most
     obvious telltale (`navigator.webdriver === true`). Combined with the
     init script in `_STEALTH_INIT_JS` it gets us past the cheap detectors.
  3. Real system Chrome (`executable_path`) is preferred over Playwright's
     bundled Chromium. The bundled build has a slightly different feature
     set + version that fingerprinters flag.

CRITICAL THREADING NOTE: Playwright's sync API can't run inside an asyncio
event loop. NiceGUI / FastAPI run uvicorn with a live loop, so any sync
Playwright call MUST be wrapped in `asyncio.to_thread(...)` — or the call
must originate from a place that has no loop (e.g. a Qt worker thread).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Iterator

log = logging.getLogger("egon.scraper")

# Each adapter's persistent profile lives here.
STATE_ROOT = Path(__file__).resolve().parent.parent / "state" / "browser"

# Common Chrome install locations on Windows, probed at launch.
# We prefer the user's real Chrome over Playwright's bundled Chromium —
# its version, plugins, and UA all match what the rest of the web sees from
# this machine, so bot-detection heuristics light up far less often.
_CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    # Microsoft Edge as a fallback (Chromium-based, also works with Playwright)
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

# Realistic UA matching a modern Windows Chrome.
_REALISTIC_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                 "AppleWebKit/537.36 (KHTML, like Gecko) "
                 "Chrome/130.0.0.0 Safari/537.36")

# Flags passed to every Chromium launch we do.
# `--disable-blink-features=AutomationControlled` is the single most important
# one: it stops Chrome from broadcasting `navigator.webdriver === true`,
# which is what >90 % of bot-detection scripts gate on first.
_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--no-default-browser-check",
    "--no-first-run",
    "--password-store=basic",
    "--use-mock-keychain",
    "--start-maximized",
]

# This script runs in the page context *before* any site script. It plugs the
# remaining detection holes left after the launch-flag fix:
#   - navigator.webdriver still exists in some versions even with the flag;
#     we forcibly redefine it to undefined.
#   - chrome.runtime exists in real Chrome with plugins; we stub it.
#   - languages / plugins / hardwareConcurrency are sometimes missing in
#     Playwright; we set defaults that match a normal install.
_STEALTH_INIT_JS = r"""
(() => {
  // Hide that we were spawned by automation.
  if (Object.getOwnPropertyDescriptor(navigator, 'webdriver')) {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
  }
  // Real Chrome has these — Playwright's blank profile sometimes doesn't.
  try { Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] }); } catch (e) {}
  try {
    Object.defineProperty(navigator, 'plugins', {
      get: () => [
        { name: 'PDF Viewer' }, { name: 'Chrome PDF Viewer' },
        { name: 'Chromium PDF Viewer' }, { name: 'Microsoft Edge PDF Viewer' },
        { name: 'WebKit built-in PDF' },
      ],
    });
  } catch (e) {}
  // Simulate Notification API
  if (window.Notification) {
    const orig = window.Notification.permission;
    Object.defineProperty(window.Notification, 'permission', { get: () => orig || 'default' });
  }
  // Some sites check window.chrome existence
  if (!window.chrome) window.chrome = { runtime: {}, csi: () => ({}), loadTimes: () => ({}) };
})();
"""


def _find_system_browser() -> str | None:
    """Return the first existing path to a system Chromium-based browser, or
    None if neither Chrome nor Edge is installed (in which case we'll fall
    back to Playwright's bundled Chromium and hope for the best)."""
    for p in _CHROME_CANDIDATES:
        if p and Path(p).exists():
            return p
    return None


def _profile_dir(name: str) -> Path:
    """Persistent Chrome profile directory for this adapter."""
    d = STATE_ROOT / name / "profile"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _state_dir(name: str) -> Path:
    """Compat shim — old code stored a state.json here; we still write one
    after interactive login so `is_logged_in()` can quickly check."""
    d = STATE_ROOT / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def is_logged_in(name: str) -> bool:
    """True if this adapter has a persistent profile with non-trivial
    cookies in it. We treat any profile directory with a Default/Cookies
    file present as 'logged in'; the next snapshot call confirms by
    attempting an authenticated request."""
    profile = _profile_dir(name)
    # Playwright stores cookies under Default/Network/Cookies (sqlite)
    cookies = profile / "Default" / "Network" / "Cookies"
    if cookies.exists() and cookies.stat().st_size > 4096:
        return True
    # Legacy state.json fallback — older sessions saved here.
    return (_state_dir(name) / "state.json").exists()


def _launch_persistent(p, name: str, headless: bool):
    """Launch a persistent Chromium context for `name`.

    `launch_persistent_context` returns a BrowserContext directly (no
    separate Browser object) and keeps every cookie/extension/local-storage
    item on disk between runs.

    Returns the BrowserContext. Caller is responsible for closing it.
    """
    profile = str(_profile_dir(name))
    exe = _find_system_browser()
    kwargs = dict(
        user_data_dir=profile,
        headless=headless,
        timeout=30000,
        args=_LAUNCH_ARGS if not headless else
            [a for a in _LAUNCH_ARGS if a != "--start-maximized"],
        viewport={"width": 1480, "height": 920},
        user_agent=_REALISTIC_UA,
        # Playwright's defaults include --enable-automation AND --no-sandbox;
        # both are visible giveaways. --enable-automation lights up
        # navigator.webdriver, and --no-sandbox makes Chrome show a yellow
        # "stability and security will suffer" banner that is itself a
        # bot-tell to anything watching DOM changes.
        ignore_default_args=["--enable-automation", "--no-sandbox"],
        accept_downloads=True,
    )
    if exe:
        kwargs["executable_path"] = exe
        log.info("launch_persistent_context(%s) using system Chrome at %s", name, exe)
    else:
        log.info("launch_persistent_context(%s) using bundled Chromium", name)
    try:
        ctx = p.chromium.launch_persistent_context(**kwargs)
    except Exception as e:
        # If the system Chrome path fails (e.g. Edge version mismatch),
        # retry without the explicit binary so Playwright picks its bundled one.
        log.warning("launch_persistent_context failed (%s) — retrying with bundled", e)
        kwargs.pop("executable_path", None)
        ctx = p.chromium.launch_persistent_context(**kwargs)
    # Inject stealth init script — runs before any site script in every page.
    try:
        ctx.add_init_script(_STEALTH_INIT_JS)
    except Exception as e:
        log.warning("could not add stealth init script: %s", e)
    return ctx


def _interactive_login_blocking(name: str, url: str, wait_message: str | None,
                                 wait_url_contains: str | None,
                                 max_wait_seconds: int) -> dict:
    """Open a real browser window, user logs in by hand, save state when done.

    Returns {status, error?}. The persistent profile under
    `state/browser/<name>/profile/` keeps cookies forever; we also write
    `state.json` for backwards-compat with anything that still looks at it.
    """
    from playwright.sync_api import sync_playwright
    state_file = _state_dir(name) / "state.json"
    try:
        with sync_playwright() as p:
            ctx = _launch_persistent(p, name, headless=False)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            except Exception as e:
                # Some sites stall on networkidle; we don't care, the user
                # will see the page just fine.
                log.info("initial goto(%s) returned %s — continuing anyway", url, e)
            if wait_message:
                try:
                    page.evaluate(
                        f'''document.title = {json.dumps(f"[Egon] {wait_message}")};'''
                    )
                except Exception:
                    pass
            deadline = time.time() + max_wait_seconds
            try:
                while time.time() < deadline:
                    try:
                        if wait_url_contains:
                            if isinstance(wait_url_contains, (list, tuple)):
                                if any(x in (page.url or "") for x in wait_url_contains):
                                    break
                            elif wait_url_contains in (page.url or ""):
                                    break
                    except Exception:
                        # Page may be navigating; ignore transient read errors
                        pass
                    if page.is_closed():
                        break
                    time.sleep(1)
            except Exception as e:
                log.info("interactive_login (%s) ended: %s", name, e)
            # Persist cookie jar (backwards-compat with adapters that still
            # call storage_state). The persistent profile is the real source
            # of truth from here on.
            try:
                state = ctx.storage_state()
                state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
            except Exception as e:
                if not state_file.exists():
                    log.warning("could not save state.json: %s", e)
            try:
                ctx.close()
            except Exception:
                pass
            return {"status": "ok", "state": str(state_file)}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"[:200]}


def interactive_login(name: str, url: str, wait_message: str | None = None,
                       wait_url_contains: str | None = None,
                       max_wait_seconds: int = 600) -> dict:
    """Sync-safe wrapper. Detects if we're inside an event loop and bounces
    the Playwright sync call into a worker thread when so."""
    try:
        asyncio.get_running_loop()
        in_loop = True
    except RuntimeError:
        in_loop = False
    if in_loop:
        # We can't run sync_playwright inside a loop; off to a thread.
        import threading
        result: dict = {"status": "error", "error": "thread did not start"}
        def _runner():
            nonlocal result
            result = _interactive_login_blocking(name, url, wait_message,
                                                  wait_url_contains, max_wait_seconds)
        t = threading.Thread(target=_runner, daemon=True)
        t.start()
        t.join(timeout=max_wait_seconds + 30)
        return result
    return _interactive_login_blocking(name, url, wait_message,
                                        wait_url_contains, max_wait_seconds)


async def interactive_login_async(name: str, url: str,
                                   wait_message: str | None = None,
                                   wait_url_contains: str | None = None,
                                   max_wait_seconds: int = 600) -> dict:
    """Preferred async entry point — `await scraper.interactive_login_async(...)`."""
    return await asyncio.to_thread(
        _interactive_login_blocking, name, url, wait_message,
        wait_url_contains, max_wait_seconds,
    )


@contextlib.contextmanager
def browser_context(name: str, headless: bool = True) -> Iterator:
    """Yield a Playwright persistent context with the saved login profile.

    Use as:
        with scraper.browser_context("paperpile", headless=True) as ctx:
            page = ctx.new_page()
            page.goto(...)
    """
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = _launch_persistent(p, name, headless=headless)
        try:
            yield ctx
        finally:
            try: ctx.close()
            except Exception: pass


def revoke(name: str) -> dict:
    """Wipe the saved login state — user will need to log in again. We
    delete BOTH the persistent profile and the legacy state.json so it
    really starts from scratch.
    """
    import shutil
    profile = _profile_dir(name)
    if profile.exists():
        shutil.rmtree(profile, ignore_errors=True)
    state_file = _state_dir(name) / "state.json"
    if state_file.exists():
        state_file.unlink()
    return {"status": "ok"}
