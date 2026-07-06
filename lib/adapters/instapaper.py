"""Instapaper adapter — Simple HTTP API.

https://www.instapaper.com/api/simple

Credentials live in egon-config.json:
    {
      "instapaper": {"username": "...", "password": "..."}
    }

Or via env vars: INSTAPAPER_USERNAME, INSTAPAPER_PASSWORD.

This adapter is intentionally read-mostly:
- live_status()    → unread count + last activity
- recent(n=20)     → list of {title, url, time, folder}
- archive(id)      → write action (one-way, undoable via Instapaper UI)
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from pathlib import Path

# httpx is heavy (~1.7s import with its rich/pygments extras) and this module
# is pulled in by lib.adapters at Egon boot. Defer it to first use.
# Bruno 2026-06-11 startup-perf pass.
import importlib


class _LazyHttpx:
    _mod = None

    def __getattr__(self, name):
        if _LazyHttpx._mod is None:
            _LazyHttpx._mod = importlib.import_module("httpx")
        return getattr(_LazyHttpx._mod, name)


httpx = _LazyHttpx()

from lib.ledger import load_config
from lib.snapshot_store import latest_snapshot

API_BASE = "https://www.instapaper.com/api"
FULL_API = "https://www.instapaper.com/api/1.1"  # OAuth-only; not used here

META = {
    "id": "instapaper",
    "label": "Instapaper",
    "icon": "📰",
    "kind": "artifact",
    "needs_auth": True,
    "destructive_actions": ["archive"],
    "read_only_default": False,
}


def _creds() -> tuple[str | None, str | None]:
    u = os.environ.get("INSTAPAPER_USERNAME")
    p = os.environ.get("INSTAPAPER_PASSWORD")
    if u and p:
        return u, p
    cfg = load_config().get("instapaper") or {}
    return cfg.get("username"), cfg.get("password")


def _authed() -> bool:
    u, p = _creds()
    return bool(u and p)


def authenticate(timeout: float = 8.0) -> dict:
    """Verify credentials via /api/authenticate. 200 = good. Surface real errors."""
    u, p = _creds()
    if not u:
        return {"status": "error", "error": "Email/username field is empty in Settings."}
    if not p:
        return {"status": "error", "error": "Password field is empty in Settings."}
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.post(f"{API_BASE}/authenticate", data={"username": u, "password": p})
        if r.status_code == 200:
            return {"status": "ok", "note": f"Authenticated as {u}"}
        if r.status_code == 401:
            return {"status": "error",
                    "error": f"Wrong username or password (Instapaper returned 401)."}
        if r.status_code == 403:
            return {"status": "error",
                    "error": "403 — Instapaper rate-limited this IP. Wait a minute and retry."}
        if r.status_code == 500:
            return {"status": "error",
                    "error": "500 — Instapaper API is having issues right now. Try again later."}
        return {"status": "error",
                "error": f"HTTP {r.status_code}: {(r.text or '(empty body)')[:180]}"}
    except httpx.TimeoutException:
        return {"status": "error", "error": f"Timed out after {timeout}s. Network or Instapaper slow."}
    except httpx.ConnectError as e:
        return {"status": "error", "error": f"Can't reach Instapaper: {e}"}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


def _harvested_count() -> tuple[int | None, str | None]:
    """(count, last_synced_iso) of the saved library from the Chrome-extension
    harvest. The Simple API has no list endpoint and the Full API needs OAuth,
    so the real reading list comes via the extension → Panop. We read it from
    Panop's live endpoint, falling back to the on-disk harvest state so the
    count shows even if Panop is momentarily down. Bruno 2026-05-29."""
    # 1) Panop live endpoint.
    try:
        r = httpx.get("http://127.0.0.1:8000/api/v1/instapaper/library", timeout=3.0)
        if r.status_code == 200:
            d = r.json() or {}
            if d.get("status") == "ok":
                cnt = d.get("count") or len(d.get("items") or [])
                return cnt, d.get("received_at")
    except Exception:
        pass
    # 2) On-disk harvest state.
    try:
        import json
        state = (Path(__file__).resolve().parent.parent.parent
                 / "state" / "panop" / "instapaper_library_state.json")
        if state.exists():
            d = json.loads(state.read_text(encoding="utf-8"))
            return d.get("count"), d.get("received_at")
    except Exception:
        pass
    return None, None


def live_status(timeout: float = 5.0) -> dict:
    """Return {queue_count, last_activity_iso, status} for the Inbox/Home strip."""
    if not _authed():
        return {"status": "unconfigured", "error": "set INSTAPAPER_USERNAME/PASSWORD or add `instapaper` block to egon-config.json"}
    auth = authenticate(timeout=timeout)
    if auth.get("status") != "ok":
        # Auth slow/failed — the saved list comes from the extension harvest,
        # not this login, so don't degrade a fresh source. Bruno 2026-07-06.
        try:
            from lib.source_health import has_recent_data
            if has_recent_data("instapaper"):
                return {"status": "ok", "source": "snapshot_cache",
                        "note": "cached saved list (live auth slow)"}
        except Exception:
            pass
        return auth
    count, last_synced = _harvested_count()
    if count is None:
        return {
            "status": "ok",
            "queue_count": None,
            "note": "auth ok · open Instapaper in Chrome (with the Egon "
                    "extension) to harvest your saved list",
        }
    return {
        "status": "ok",
        "queue_count": count,
        "last_activity_iso": last_synced,
        "note": f"{count} saved · harvested via extension"
                + (f" · {last_synced[:10]}" if last_synced else ""),
    }


def _standardize_date(date_str: str | None) -> str:
    """Parse relative and absolute date strings (English & Portuguese) to ISO format YYYY-MM-DD."""
    if not date_str:
        return datetime.now().date().isoformat()
    
    date_str = date_str.strip().lower()
    if not date_str:
        return datetime.now().date().isoformat()
        
    now = datetime.now()
    
    # 1. Simple relative words
    if date_str in ("today", "hoje", "just now", "agora mesmo"):
        return now.date().isoformat()
    if date_str in ("yesterday", "ontem"):
        return (now - timedelta(days=1)).date().isoformat()
        
    # 2. Relative times: "X days/hours/minutes ago" or "há X dias/horas/minutos"
    m_days_en = re.search(r'(\d+)\s*(?:days?|d)\s+ago', date_str)
    if m_days_en:
        return (now - timedelta(days=int(m_days_en.group(1)))).date().isoformat()
        
    m_weeks_en = re.search(r'(\d+)\s*(?:weeks?|w)\s+ago', date_str)
    if m_weeks_en:
        return (now - timedelta(days=int(m_weeks_en.group(1)) * 7)).date().isoformat()

    m_months_en = re.search(r'(\d+)\s*(?:months?|mo|mths?)\s+ago', date_str)
    if m_months_en:
        return (now - timedelta(days=int(m_months_en.group(1)) * 30)).date().isoformat()

    m_years_en = re.search(r'(\d+)\s*(?:years?|y|yrs?)\s+ago', date_str)
    if m_years_en:
        return (now - timedelta(days=int(m_years_en.group(1)) * 365)).date().isoformat()
        
    m_hours_en = re.search(r'(\d+)\s*(?:hours?|h)\s+ago', date_str)
    if m_hours_en:
        return (now - timedelta(hours=int(m_hours_en.group(1)))).date().isoformat()
        
    m_mins_en = re.search(r'(\d+)\s*(?:minutes?|mins?|m)\s+ago', date_str)
    if m_mins_en:
        return (now - timedelta(minutes=int(m_mins_en.group(1)))).date().isoformat()

    # Portuguese relative
    m_days_pt = re.search(r'há\s+(\d+)\s*(?:dias|d)', date_str)
    if m_days_pt:
        return (now - timedelta(days=int(m_days_pt.group(1)))).date().isoformat()
        
    m_weeks_pt = re.search(r'há\s+(\d+)\s*(?:semanas?|sem|w)', date_str)
    if m_weeks_pt:
        return (now - timedelta(days=int(m_weeks_pt.group(1)) * 7)).date().isoformat()

    m_months_pt = re.search(r'há\s+(\d+)\s*(?:meses|mês|mth|mo)', date_str)
    if m_months_pt:
        return (now - timedelta(days=int(m_months_pt.group(1)) * 30)).date().isoformat()

    m_years_pt = re.search(r'há\s+(\d+)\s*(?:anos?|y)', date_str)
    if m_years_pt:
        return (now - timedelta(days=int(m_years_pt.group(1)) * 365)).date().isoformat()
        
    m_hours_pt = re.search(r'há\s+(\d+)\s*(?:horas|h)', date_str)
    if m_hours_pt:
        return (now - timedelta(hours=int(m_hours_pt.group(1)))).date().isoformat()
        
    m_mins_pt = re.search(r'há\s+(\d+)\s*(?:minutos|min|m)', date_str)
    if m_mins_pt:
        return (now - timedelta(minutes=int(m_mins_pt.group(1)))).date().isoformat()

    # Generic short format: "2d", "3h", "5m", "4w", "1y"
    m_short = re.search(r'^(\d+)([dhmwy])$', date_str)
    if m_short:
        val = int(m_short.group(1))
        unit = m_short.group(2)
        if unit == 'd':
            return (now - timedelta(days=val)).date().isoformat()
        elif unit == 'h':
            return (now - timedelta(hours=val)).date().isoformat()
        elif unit == 'm':
            return (now - timedelta(minutes=val)).date().isoformat()
        elif unit == 'w':
            return (now - timedelta(days=val * 7)).date().isoformat()
        elif unit == 'y':
            return (now - timedelta(days=val * 365)).date().isoformat()

    m_short_mo = re.search(r'^(\d+)mo$', date_str)
    if m_short_mo:
        return (now - timedelta(days=int(m_short_mo.group(1)) * 30)).date().isoformat()

    # 3. Absolute dates
    pt_months = {
        "janeiro": 1, "fevereiro": 2, "março": 3, "abril": 4, "maio": 5, "junho": 6,
        "julho": 7, "agosto": 8, "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12,
        "jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
        "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12
    }
    en_months = {
        "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
        "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12
    }

    m_pt_abs = re.search(r'(\d+)\s+de\s+([a-z]+)(?:\s+de|,)?\s+(\d{4})', date_str)
    if m_pt_abs:
        day = int(m_pt_abs.group(1))
        month_str = m_pt_abs.group(2)
        year = int(m_pt_abs.group(3))
        month = pt_months.get(month_str, 1)
        try:
            return datetime(year, month, day).date().isoformat()
        except ValueError:
            pass

    m_en_abs1 = re.search(r'([a-z]+)\s+(\d+)(?:st|nd|rd|th)?,?\s+(\d{4})', date_str)
    if m_en_abs1:
        month_str = m_en_abs1.group(1)
        day = int(m_en_abs1.group(2))
        year = int(m_en_abs1.group(3))
        month = en_months.get(month_str, 1)
        try:
            return datetime(year, month, day).date().isoformat()
        except ValueError:
            pass

    m_en_abs2 = re.search(r'(\d+)(?:st|nd|rd|th)?\s+([a-z]+)\s+(\d{4})', date_str)
    if m_en_abs2:
        day = int(m_en_abs2.group(1))
        month_str = m_en_abs2.group(2)
        year = int(m_en_abs2.group(3))
        month = en_months.get(month_str, 1)
        try:
            return datetime(year, month, day).date().isoformat()
        except ValueError:
            pass

    m_en_abs3 = re.search(r'([a-z]+)\s+(\d+)(?:st|nd|rd|th)?', date_str)
    if m_en_abs3:
        month_str = m_en_abs3.group(1)
        day = int(m_en_abs3.group(2))
        month = en_months.get(month_str, 1)
        try:
            return datetime(now.year, month, day).date().isoformat()
        except ValueError:
            pass

    m_iso = re.search(r'(\d{4})[./-](\d{1,2})[./-](\d{1,2})', date_str)
    if m_iso:
        try:
            return datetime(int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3))).date().isoformat()
        except ValueError:
            pass

    return now.date().isoformat()


def _harvest_items() -> list[dict]:
    # 1) Panop live endpoint (freshest — populated while the extension posts).
    try:
        r = httpx.get("http://127.0.0.1:8000/api/v1/instapaper/library", timeout=2.0)
        if r.status_code == 200:
            data = r.json() or {}
            if data.get("status") == "ok" and data.get("items"):
                return data.get("items") or []
    except Exception:
        pass
    # 2) Fallback to the persisted harvest state on disk. Bruno 2026-05-29:
    # Panop does NOT rehydrate its in-memory harvest store on restart, so the
    # endpoint returns no_data until the extension re-posts — even though the
    # last harvest (3000+ items) is sitting in state/panop/. Read it directly
    # so the saved list survives Egon restarts.
    try:
        import json
        state = (Path(__file__).resolve().parent.parent.parent
                 / "state" / "panop" / "instapaper_library_state.json")
        if state.exists():
            d = json.loads(state.read_text(encoding="utf-8"))
            return d.get("items") or []
    except Exception:
        pass
    return []


def _dedup_key(it: dict) -> str:
    """Key that collapses RE-ADDS of the same article. The real article URL
    is best, but Instapaper sometimes stores a generic app-link redirect
    (e.g. substack app-link/post?isFreemail=true) shared across articles —
    for those, fall back to the normalized title. Bruno 2026-06-13."""
    url = (it.get("url") or "").strip().lower()
    generic = (not url or "app-link" in url or url.endswith("isfreemail=true")
               or url.count("/") < 3)
    if not generic:
        return url.split("?")[0].rstrip("/")
    title = (it.get("title") or "").strip().lower()
    return "t:" + title if title else "id:" + str(it.get("id") or "")


def snapshot() -> dict:
    """Harvest from Panop, DEDUP re-adds (keep newest), sort by recency.

    Instapaper's web DOM exposes no per-item date, so `time` is empty — but
    the bookmark `id` is MONOTONIC (higher = saved more recently), a reliable
    recency key. Real timestamps arrive via the CSV export (Settings →
    Export), parsed by lib/export_inbox. Bruno 2026-06-13: needs recency sort
    + dedup of re-added articles."""
    raw_items = _harvest_items()

    best: dict[str, dict] = {}
    for it in raw_items:
        try:
            iid = int(str(it.get("id") or "0"))
        except Exception:
            iid = 0
        k = _dedup_key(it)
        prev = best.get(k)
        if prev is None or iid > prev["_iid"]:
            best[k] = {**it, "_iid": iid}

    items_out = []
    for it in best.values():
        iid = it.pop("_iid", 0)
        time_str = it.get("time")
        items_out.append({
            "id": it.get("id"),
            "title": it.get("title"),
            "url": it.get("url"),
            "read_url": it.get("read_url"),
            "host": it.get("host"),
            "time": _standardize_date(time_str),   # filled once a CSV export lands
            "added_ord": iid,                       # monotonic recency key
            "raw_time": time_str,
            "description": it.get("description"),
            "folder": it.get("folder"),
            "source": it.get("source"),
        })

    items_out.sort(key=lambda x: (x.get("time") or "", x.get("added_ord") or 0),
                   reverse=True)
    return {
        "status": "ok",
        "synced_at": datetime.now().isoformat(),
        "count": len(items_out),
        "deduped_from": len(raw_items),
        "items": items_out,
    }


def items(limit: int = 5000) -> list[dict]:
    """Read cached items from the latest snapshot, falling back to direct Panop harvest."""
    snap = latest_snapshot(META["id"])
    if snap and snap.get("status") == "ok":
        return snap.get("items", [])[:limit]
    return _harvest_items()[:limit]


def stats() -> dict:
    snap = latest_snapshot(META["id"])
    if not snap:
        return {"status": "no-snapshot", "count": 0, "last_synced": None,
                "error": "click Sync now to pull first snapshot"}
    return {
        "status": snap.get("status", "ok"),
        "count": snap.get("count", 0),
        "last_synced": (snap.get("synced_at") or "")[:16],
    }


def add_bookmark(url: str, title: str | None = None,
                 description: str | None = None,
                 timeout: float = 8.0) -> dict:
    """Add a URL to Instapaper. This is the most-useful write action."""
    if not _authed():
        return {"status": "error", "error": "not configured"}
    u, p = _creds()
    data = {"username": u, "password": p, "url": url}
    if title:        data["title"] = title
    if description:  data["selection"] = description
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.post(f"{API_BASE}/add", data=data)
        if r.status_code == 201:
            return {"status": "ok", "id": r.headers.get("Instapaper-Bookmark-Id")}
        return {"status": "error", "error": f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as e:
        return {"status": "error", "error": str(e)}
