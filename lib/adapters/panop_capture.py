"""Panop capture orchestrator — runs entirely inside Egon, no external Panop dependency.

End-to-end:
  1. Discover phone over wireless ADB (mDNS first, static IP fallback)
  2. Prune duplicate adb device entries; pin a single canonical target
  3. Wake screen + foreground Chrome on the phone
  4. Forward Chrome's DevTools socket to localhost:9222
  5. Run Panop's drain pipeline (the vendored, AI-augmented version) which:
       - Fetches /json/list
       - Phase A: process tabs with visible URLs
       - Phase B: batch-wake suspended tabs (groups of 40, 3 rounds × 4 polls)
       - For each tab: try domain+body match → fall back to AI prediction
                       (bag-of-words against learned profiles) → if no match, skip
       - Save matched: local JSON + Zotero + (queued) Chrome bookmark
       - Close ONLY if z_synced AND b_synced AND cat_id is real (hard code-level gate)
  6. Log outcome to logs/panop-YYYY-MM.log

State (config, history, AI profiles) all live in `egon/state/panop/`.
Zotero credentials sourced from `egon-local/config/connectors.env` via `lib.secrets`.
Never raises; degrades to "skipped" when the phone is unreachable.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from lib import secrets
from lib import restore_points

ROOT = Path(__file__).resolve().parents[2]
ADB_EXE = ROOT / "panop_output" / "platform-tools" / "platform-tools" / "adb.exe"
PANOP_VENDORED = ROOT / "external" / "panop_server"
PANOP_STATE_DIR = ROOT / "state" / "panop"
LOG_DIR = ROOT / "logs"
LOG_FILE = LOG_DIR / f"panop-{datetime.now():%Y-%m}.log"

# Fallback static targets if mDNS discovery fails (e.g. router DHCP keeps IP stable).
STATIC_FALLBACKS = [x for x in os.environ.get("EGON_PHONE_IP", "").split(",") if x]

# Phone's persistent device serial (set EGON_PHONE_SERIAL) — matches the prefix of
# mDNS service names like `adb-<SERIAL>-xxxx._adb-tls-connect._tcp`.
DEVICE_SERIAL = os.environ.get("EGON_PHONE_SERIAL", "")


# -- logging ------------------------------------------------------------------

def _log(level: str, event: str, **kw) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    entry = {"ts": datetime.now().isoformat(timespec="seconds"),
             "level": level, "event": event, **kw}
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _adb(*args, timeout: int = 30) -> tuple[int, str, str]:
    """Run adb with a timeout. Returns (rc, stdout, stderr). Never raises."""
    try:
        res = subprocess.run(
            [str(ADB_EXE), *args],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=timeout,
        )
        return res.returncode, res.stdout, res.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"adb timeout ({timeout}s)"
    except Exception as e:
        return 125, "", str(e)[:200]


# -- device discovery ---------------------------------------------------------

def _discover_targets(mdns_wait_s: float = 8.0) -> list[str]:
    """Return ordered list of ADB-connect targets to try.

    Priority (rewritten 2026-05-20 — adb's built-in mDNS is broken on Bruno's
    multi-NIC PC; we use Python's zeroconf library instead):

      1. **Python zeroconf** mDNS scan for `_adb-tls-connect._tcp` AND
         `_adb._tcp`. This is the reliable path; finds the phone within ~2s
         when the phone is advertising at all.
      2. **adb mdns services** as fallback (rarely works on this host, but
         cheap to try).
      3. Static IP fallbacks (last-ditch).
    """
    targets: list[str] = []

    # 1. Python-level mDNS (replaces unreliable `adb mdns services`)
    try:
        from lib.adapters.phone_discovery import find_phone_any_service
        addr = find_phone_any_service(
            serial_prefix=DEVICE_SERIAL,
            timeout_s=min(mdns_wait_s, 6.0),
        )
        if addr and addr not in targets:
            targets.insert(0, addr)
    except Exception:
        pass

    # 2. adb's mDNS — almost never works for us, but quick to try
    if not targets:
        deadline = time.time() + 2.0
        while time.time() < deadline and not targets:
            rc, out, _ = _adb("mdns", "services", timeout=5)
            if rc == 0:
                for line in out.splitlines():
                    parts = re.split(r"\s+", line.strip())
                    if len(parts) < 3: continue
                    name, service, addr = parts[0], parts[1], parts[-1]
                    if DEVICE_SERIAL not in name: continue
                    if not re.match(r"\d{1,3}(\.\d{1,3}){3}:\d+", addr): continue
                    if service.startswith("_adb-tls-connect"):
                        if addr not in targets: targets.insert(0, addr)
                    else:
                        if addr not in targets: targets.append(addr)
                if targets: break
            time.sleep(0.5)

    # 3. Static fallback
    for f in STATIC_FALLBACKS:
        if f not in targets:
            targets.append(f)

    return targets


def _ensure_connected() -> tuple[bool, str]:
    """Connect to the phone over wifi. Returns (ok, target) where target is
    a SINGLE canonical adb device id (IP:port).

    Important: ADB lists the same physical phone under multiple entries (USB
    serial, mDNS service name, plain TCP target). Bare `adb forward` from
    Panop fails with "more than one device" in that case. So after connecting,
    we disconnect every entry except the one we settled on, leaving exactly
    one device visible.
    """
    _adb("start-server", timeout=10)

    chosen: str | None = None

    # FAST PATH: if a device is already connected and online (e.g. connected
    # manually, or persisting from a prior run), use it directly. mDNS can be
    # flaky on multi-adapter PCs, so don't depend on rediscovery when there's
    # already a live device.
    rc, devs, _ = _adb("devices", timeout=10)
    if rc == 0:
        for line in devs.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == "device":
                entry = parts[0]
                # Prefer a real IP:port or mDNS-tls entry over a bare USB serial
                if ":" in entry or "_adb" in entry:
                    chosen = entry
                    break
        if not chosen:
            # accept even a USB-serial device if that's all we have
            for line in devs.splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[1] == "device":
                    chosen = parts[0]
                    break

    for target in ([] if chosen else _discover_targets()):
        rc, out, err = _adb("connect", target, timeout=15)
        if rc != 0:
            continue
        text = (out + err).lower()
        if "connected" not in text and "already" not in text:
            continue
        rc2, devs, _ = _adb("devices", timeout=10)
        if rc2 == 0 and re.search(rf"{re.escape(target)}\s+device\b", devs):
            chosen = target
            break

    if not chosen:
        return False, "no reachable adb target (phone off network or wifi debugging off)"

    # Prune redundant entries so Panop's `adb forward` (which doesn't pass -s)
    # has exactly one device to talk to.
    rc, devs, _ = _adb("devices", timeout=10)
    if rc == 0:
        for line in devs.splitlines():
            parts = line.split()
            if len(parts) < 2 or parts[1] != "device":
                continue
            entry = parts[0]
            if entry == chosen:
                continue
            # Skip USB serials (no ':' in them and not an mDNS-style name)
            if ":" not in entry and "_adb" not in entry:
                continue
            _adb("disconnect", entry, timeout=10)

    return True, chosen


def _read_current_power_settings(target: str) -> tuple[str, str]:
    """Read current screen_off_timeout and stay_on_while_plugged_in settings from the phone."""
    _, timeout_val, _ = _adb("-s", target, "shell", "settings", "get", "system", "screen_off_timeout", timeout=8)
    _, stayon_val, _ = _adb("-s", target, "shell", "settings", "get", "global", "stay_on_while_plugged_in", timeout=8)
    
    timeout_val = (timeout_val or "").strip()
    stayon_val = (stayon_val or "").strip()
    
    # Safety fallback: if values are empty or invalid
    if not timeout_val or timeout_val == "null":
        timeout_val = "120000"  # default to 2 mins
    if not stayon_val or stayon_val == "null":
        stayon_val = "0"
        
    # Healing logic: if screen timeout is excessively large, reset to a sane 2-minute default
    try:
        val_int = int(timeout_val)
        if val_int > 1800000:  # > 30 minutes
            timeout_val = "120000"
    except ValueError:
        timeout_val = "120000"
        
    return timeout_val, stayon_val


def _restore_power_settings(target: str, timeout_val: str, stayon_val: str) -> None:
    """Restore the phone's original screen timeout and stay-awake settings."""
    _adb("-s", target, "shell", "settings", "put", "system", "screen_off_timeout", timeout_val, timeout=8)
    if stayon_val == "0":
        _adb("-s", target, "shell", "svc", "power", "stayon", "false", timeout=8)
    else:
        _adb("-s", target, "shell", "settings", "put", "global", "stay_on_while_plugged_in", stayon_val, timeout=8)
    _log("info", "phone_power_settings_restored", target=target, timeout=timeout_val, stayon=stayon_val)


def _keep_phone_awake(target: str) -> None:
    """Sets a safe temporary 10-minute screen timeout and stay-on-power during drain.
    Does not persist permanently — restored to original values immediately when done."""
    _adb("-s", target, "shell", "settings", "put", "system",
         "screen_off_timeout", "600000", timeout=8)  # 10 minutes (safe)
    _adb("-s", target, "shell", "svc", "power", "stayon", "true", timeout=8)
    _log("info", "phone_keep_awake_asserted", target=target)


def _wake_and_open_chrome(target: str) -> None:
    """Wake the screen and bring Chrome to foreground so the DevTools socket is live."""
    _adb("-s", target, "shell", "input", "keyevent", "KEYCODE_WAKEUP", timeout=10)
    # Lock in temporary keep-awake BEFORE opening Chrome
    _keep_phone_awake(target)
    # Bringing Chrome forward via explicit component
    _adb("-s", target, "shell", "am", "start",
         "-n", "com.android.chrome/com.google.android.apps.chrome.Main",
         timeout=15)
    time.sleep(3)


# -- Panop invocation ---------------------------------------------------------

def _import_panop():
    """Import the vendored Panop module (lazy) and configure it to use Egon's state."""
    if str(PANOP_VENDORED) not in sys.path:
        sys.path.insert(0, str(PANOP_VENDORED))
    import main as panop_main  # type: ignore

    # Point Panop at its env file (which already points root_dir to egon/state/panop)
    panop_main.ENV_FILE = str(PANOP_VENDORED / "panop_env.json")

    # Inject Zotero creds at runtime — never written to the panop_env.json on disk.
    # Panop's send_to_zotero() reads creds from env via get_env(); we patch the
    # returned dict so that function call returns our secrets-sourced values.
    real_get_env = panop_main.get_env
    def _get_env_with_creds():
        e = real_get_env()
        e["zotero_api_key"] = secrets.get("zotero.api_key", "") or ""
        e["zotero_user_id"] = secrets.get("zotero.user_id", "") or ""
        return e
    panop_main.get_env = _get_env_with_creds
    return panop_main


def _run_panop_drain(adb_target: str) -> dict:
    """Drive Panop's two-phase drain (Phase A visible + Phase B batch-wake-suspended).

    Sets `ANDROID_SERIAL` so Panop's bare `adb forward` routes to the canonical
    device. Returns the final drain_status dict (saved/closed/skipped counts).
    """
    try:
        panop_main = _import_panop()
    except Exception as e:
        return {"running": False, "last_error": f"panop import failed: {e}"}

    os.environ["ANDROID_SERIAL"] = adb_target

    # Reset drain_status for this run (it's module-level, persists across imports)
    panop_main.drain_status.update({
        "running": True, "processed": 0, "saved": 0, "closed": 0,
        "skipped": 0, "remaining": 0, "current_url": "", "cancel": False,
        "last_error": "", "started_at": datetime.now().isoformat(),
        "finished_at": None, "phase": "starting", "total_initial": 0,
    })
    try:
        # `_process_all_tabs_loop` is the heavyweight drain — handles thousands of
        # suspended tabs with resumable state, AI-fallback classification, and the
        # hard `_safe_to_close` gate.
        panop_main._process_all_tabs_loop(resume=True)
    except Exception as e:
        panop_main.drain_status["last_error"] = str(e)[:300]
    finally:
        panop_main.drain_status["running"] = False
    return dict(panop_main.drain_status)


# -- entry point --------------------------------------------------------------

_DAY_FLAG = ROOT / "state" / "panop" / "last_successful_drain.flag"


def _already_ran_today() -> bool:
    """True if a drain has already completed successfully today.

    Used so the scheduled task can fire multiple times (06:30, 07:30, ...)
    for reliability — first successful run wins, subsequent attempts no-op.
    """
    try:
        if not _DAY_FLAG.exists():
            return False
        marker = _DAY_FLAG.read_text(encoding="utf-8").strip()[:10]
        return marker == datetime.now().strftime("%Y-%m-%d")
    except Exception:
        return False


def _mark_ran_today() -> None:
    try:
        _DAY_FLAG.parent.mkdir(parents=True, exist_ok=True)
        _DAY_FLAG.write_text(datetime.now().isoformat(timespec="seconds"), encoding="utf-8")
    except Exception:
        pass


def run_capture() -> int:
    """Top-level orchestration. Returns 0 on success (incl. graceful skip), 1 on hard error."""
    _log("info", "capture_start")

    # G7 — intra-day retry: if today's drain already finished, no-op.
    # The scheduled task fires multiple times so the first reachable-phone
    # attempt wins. Subsequent attempts hit this guard and exit immediately.
    if _already_ran_today():
        _log("info", "already_ran_today", flag=str(_DAY_FLAG))
        return 0

    # SAFETY: snapshot every writable state file BEFORE the sweep can touch
    # anything. If anything goes wrong (misclassification, accidental closure,
    # poisoned AI profile), `python scripts/restore_point.py restore <id>`
    # brings the previous state back. Per Bruno 2026-05-15 directive.
    try:
        rp = restore_points.create("panop_drain", reason="nightly_run_pre_capture")
        _log("info", "restore_point_created", point_id=rp.get("point_id"))
    except Exception as e:
        _log("warn", "restore_point_failed", error=str(e)[:200])

    if not ADB_EXE.exists():
        _log("error", "adb_missing", path=str(ADB_EXE))
        return 1

    ok, detail = _ensure_connected()
    if not ok:
        _log("info", "phone_unreachable", detail=detail)
        return 0  # not an error — just nothing to do tonight

    _log("info", "phone_connected", target=detail)
    
    # Read the user's current settings before we alter them
    orig_timeout, orig_stayon = _read_current_power_settings(detail)
    try:
        _wake_and_open_chrome(detail)
        status = _run_panop_drain(detail)
        _log("info", "drain_done",
             total_initial=status.get("total_initial"),
             processed=status.get("processed"),
             saved=status.get("saved"),
             closed=status.get("closed"),
             skipped=status.get("skipped"),
             phase=status.get("phase"),
             error=status.get("last_error") or None)

        # G7 — mark today done so the next intra-day retry attempt no-ops.
        # A drain is "successful enough" if it finished its phases without a fatal
        # error, even if some tabs were skipped (those retry next day naturally).
        if not status.get("last_error"):
            _mark_ran_today()
    finally:
        # Guarantee settings restore even on failure/timeout
        _restore_power_settings(detail, orig_timeout, orig_stayon)

    return 0


def test_one_tab() -> int:
    """Single-tab smoke test. Picks the FIRST not-yet-in-history tab from the
    phone's Chrome that domain-matches a category, runs the full classify→Zotero
    push pipeline on that one tab only, and reports the outcome.

    Verifies the wiring end-to-end (mDNS connect, page fetch, classification,
    Zotero auth, history write) before letting the batch drain loose.
    """
    _log("info", "test_one_start")
    if not ADB_EXE.exists():
        _log("error", "adb_missing", path=str(ADB_EXE))
        return 1
    ok, target = _ensure_connected()
    if not ok:
        _log("info", "phone_unreachable", detail=target)
        return 0
    _log("info", "phone_connected", target=target)
    
    orig_timeout, orig_stayon = _read_current_power_settings(target)
    try:
        _wake_and_open_chrome(target)
        os.environ["ANDROID_SERIAL"] = target

        try:
            panop_main = _import_panop()
        except Exception as e:
            _log("error", "panop_import_failed", error=str(e))
            return 1

        # Establish DevTools forward + pull tab list
        adb_exe = panop_main.ensure_adb()
        subprocess.run([adb_exe, "forward", "--remove", "tcp:9222"], capture_output=True)
        subprocess.run([adb_exe, "forward", "tcp:9222", "localabstract:chrome_devtools_remote"],
                       capture_output=True)
        time.sleep(1)

        import requests
        try:
            tabs = requests.get("http://127.0.0.1:9222/json/list", timeout=30).json()
        except Exception as e:
            _log("error", "devtools_unreachable", error=str(e))
            return 1

        config = panop_main.load_config()
        env = panop_main.get_env()
        categories = config.get("categories", [])
        history = panop_main.load_history()
        _log("info", "tabs_found", count=len(tabs), categories=len(categories), history=len(history))

        # Find first plausible candidate: real URL, not in history, domain-matches a category
        chosen = None
        for tab in tabs:
            url = (tab.get("url") or "").strip()
            if not url or url.startswith(("chrome://", "about:", "devtools://", "chrome-native://")):
                continue
            if url in history or panop_main.canonicalize_url(url) in history:
                continue
            url_lower = url.lower()
            for cat in categories:
                domains = cat.get("domain_keywords", [])
                if any(d.lower() in url_lower for d in domains if d):
                    chosen = (tab, cat)
                    break
            if chosen:
                break

        if not chosen:
            _log("info", "no_unprocessed_match",
                 hint="every visible tab is either chrome://, already in history, or doesn't match any category domain")
            return 0

        tab, cat = chosen
        url = tab["url"]
        title = tab.get("title") or url
        tid = tab.get("id")
        _log("info", "chosen_tab", url=url, title=title[:120], category=cat["name"])

        # Run the actual classify+save on this one tab
        saved, closed = panop_main._drain_classify_and_save(url, title, categories, env, tid)
        _log("info", "test_one_done", saved=saved, closed=closed, url=url)

        # Read back the freshly-written history entry to confirm round-trip
        h = panop_main.load_history()
        key = panop_main.canonicalize_url(url) or url
        item = h.get(key) or h.get(url) or {}
        _log("info", "history_record",
             z_synced=item.get("z_synced"), b_synced=item.get("b_synced"),
             category=item.get("category"), ai_learned=item.get("ai_learned"))
    finally:
        _restore_power_settings(target, orig_timeout, orig_stayon)
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--test-one":
        raise SystemExit(test_one_tab())
    raise SystemExit(run_capture())
