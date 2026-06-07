"""Vault adapter — counts pages, mirror status, recent edits, inbox folder size.

The full scan walks 62 k+ markdown files across the Drive-mounted vault and
takes ~2 minutes cold. To keep `live_status()` callable from request handlers
(snapshot, UI refresh, etc.) we cache the result for `_CACHE_TTL_S` and do
async background refresh — same pattern as `lib.state`.

First call after a process boot returns whatever is in the on-disk cache
(or a 'warming' placeholder if no cache yet). Subsequent calls return the
cached value instantly. A background thread re-scans when the cache is stale.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path

from lib.egon_paths import VAULT_ROOT
INBOX = VAULT_ROOT / "001 - Inbox"
MIRROR_STATE = VAULT_ROOT / ".mirror_state.json"

# Cache file lives in the local egon state — Drive cache would defeat the point
_CACHE_FILE = Path(__file__).resolve().parent.parent.parent / "state" / "vault_status_cache.json"
_CACHE_TTL_S = 30 * 60       # 30 min — vault contents don't change that fast
_REFRESH_LOCK = threading.Lock()
_REFRESHING = {"on": False}


def _md_count(folder: Path) -> int:
    if not folder.exists():
        return 0
    return sum(1 for p in folder.rglob("*.md") if p.is_file())


def _last_modified(folder: Path) -> datetime | None:
    if not folder.exists():
        return None
    try:
        latest = max((p.stat().st_mtime for p in folder.rglob("*.md") if p.is_file()), default=0)
    except Exception:
        latest = 0
    return datetime.fromtimestamp(latest) if latest else None


def _slow_scan() -> dict:
    """The expensive ~2 min walk. ONLY runs in a background thread."""
    if not VAULT_ROOT.exists():
        return {"status": "error", "error": f"vault not mounted: {VAULT_ROOT}"}
    try:
        kms_pages = sum(_md_count(VAULT_ROOT / d) for d in (
            "000 - Meta", "001 - Inbox", "010 - Journal", "020 - Notes",
            "030 - Projects", "040 - Areas", "050 - Resources", "060 - Archive",
        ))
        inbox_count = _md_count(INBOX)
        last_mod = _last_modified(VAULT_ROOT / "020 - Notes")
        last_run, conflicts = None, 0
        if MIRROR_STATE.exists():
            try:
                state = json.loads(MIRROR_STATE.read_text(encoding="utf-8"))
                last_run = state.get("last_run_iso")
                conflicts = state.get("conflicts_total", 0)
            except Exception:
                pass
        return {
            "pages_mirrored": kms_pages,
            "inbox_count": inbox_count,
            "delta_24h": None,
            "last_activity_iso": last_mod.isoformat() if last_mod else None,
            "last_run_iso": last_run,
            "conflicts": conflicts,
            "status": "ok",
            "scanned_at": datetime.now().isoformat(timespec="seconds"),
        }
    except Exception as e:
        return {"status": "error", "error": str(e)[:200]}


def _load_cache() -> dict | None:
    try:
        if _CACHE_FILE.exists():
            return json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None
    return None


def _save_cache(data: dict) -> None:
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def _refresh_async() -> None:
    """Spawn the slow scan in a daemon thread; persist on completion."""
    with _REFRESH_LOCK:
        if _REFRESHING["on"]:
            return
        _REFRESHING["on"] = True

    def _bg() -> None:
        try:
            result = _slow_scan()
            result["_cache_ts"] = time.time()
            _save_cache(result)
        finally:
            with _REFRESH_LOCK:
                _REFRESHING["on"] = False

    threading.Thread(target=_bg, daemon=True, name="vault-refresh").start()


def live_status() -> dict:
    """Fast path — returns cached value, kicks off async refresh if stale.

    Worst case (no cache yet AND vault not mounted): returns a 'warming'
    placeholder. The UI shows it as 'pending' until the first background
    scan completes.
    """
    cached = _load_cache()
    now = time.time()
    if cached:
        age = now - cached.get("_cache_ts", 0)
        if age > _CACHE_TTL_S:
            _refresh_async()
        # always return cached data — even when stale we have *something*
        cached["cache_age_s"] = int(age)
        return cached
    # cold — kick a refresh and return a placeholder
    _refresh_async()
    return {
        "status": "warming",
        "error": "first vault scan in progress (~2min); UI will update on next refresh",
    }
