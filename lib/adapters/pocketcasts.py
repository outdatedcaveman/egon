"""Pocket Casts adapter — subscribed podcasts + listening history.

Pocket Casts has no *official* public API, but the same JSON API its own web
player (play.pocketcasts.com) uses is stable and well-understood. Auth is a
simple email/password → bearer-token exchange — no OAuth, no captcha, no
browser automation. This makes it the cleanest of all our media integrations.

Flow:
  1. POST /user/login  {email, password, scope: "webplayer"}  → {token}
  2. token cached in-process (refreshed on 401)
  3. POST /user/podcast/list  → subscribed podcasts (with artwork)
  4. POST /user/history       → recently played episodes

Credentials live in egon-config.json:
    { "pocketcasts": { "email": "...", "password": "..." } }

Read-only: we never modify subscriptions, queue, or playback state.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime

import httpx

from lib import secrets

API = "https://api.pocketcasts.com"
_UA = {"User-Agent": "Egon/1.0 (personal KMS; read-only)",
       "Content-Type": "application/json", "Accept": "application/json"}

META = {
    "id": "pocketcasts",
    "label": "Pocket Casts",
    "icon": "🎧",
    "kind": "media",
    "needs_auth": True,
    "destructive_actions": [],
    "read_only_default": True,
}

_TOKEN = {"value": None, "ts": 0.0, "lock": threading.Lock()}
_TOKEN_TTL_S = 60 * 60  # re-login hourly


def _creds() -> tuple[str | None, str | None]:
    return secrets.get("pocketcasts.email"), secrets.get("pocketcasts.password")


def _login(force: bool = False) -> str | None:
    """Return a bearer token, logging in if needed. Cached for an hour."""
    with _TOKEN["lock"]:
        now = time.time()
        if not force and _TOKEN["value"] and (now - _TOKEN["ts"] < _TOKEN_TTL_S):
            return _TOKEN["value"]
        email, password = _creds()
        if not (email and password):
            return None
        try:
            r = httpx.post(f"{API}/user/login", json={
                "email": email, "password": password, "scope": "webplayer",
            }, headers=_UA, timeout=15.0)
            if r.status_code == 200:
                tok = r.json().get("token")
                _TOKEN["value"] = tok
                _TOKEN["ts"] = now
                return tok
        except Exception:
            return None
    return None


def _auth_post(path: str, body: dict | None = None) -> dict | None:
    """POST to an authenticated endpoint, re-logging-in once on 401."""
    tok = _login()
    if not tok:
        return None
    for attempt in (1, 2):
        try:
            r = httpx.post(f"{API}{path}",
                           json=body or {},
                           headers={**_UA, "Authorization": f"Bearer {tok}"},
                           timeout=20.0)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 401 and attempt == 1:
                tok = _login(force=True)  # token expired — refresh once
                if not tok:
                    return None
                continue
            return None
        except Exception:
            return None
    return None


from pathlib import Path
import json

_ITUNES_CACHE = {}
_ITUNES_CACHE_FILE = Path(__file__).resolve().parent.parent.parent / "state" / "pocketcasts_artwork_cache.json"
_itunes_lock = threading.Lock()

def _load_itunes_cache():
    global _ITUNES_CACHE
    if not _ITUNES_CACHE:
        try:
            if _ITUNES_CACHE_FILE.exists():
                _ITUNES_CACHE = json.loads(_ITUNES_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            _ITUNES_CACHE = {}

def _save_itunes_cache():
    try:
        _ITUNES_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _ITUNES_CACHE_FILE.write_text(json.dumps(_ITUNES_CACHE, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass

def _resolve_itunes_artwork(title: str, default_url: str) -> str:
    if not title:
        return default_url
    _load_itunes_cache()
    if title in _ITUNES_CACHE:
        return _ITUNES_CACHE[title]
        
    try:
        import urllib.parse
        term = urllib.parse.quote(title)
        r = httpx.get(f"https://itunes.apple.com/search?term={term}&entity=podcast", timeout=5.0)
        if r.status_code == 200:
            results = r.json().get("results", [])
            if results:
                art_url = results[0].get("artworkUrl600") or results[0].get("artworkUrl100") or ""
                if art_url:
                    with _itunes_lock:
                        _ITUNES_CACHE[title] = art_url
                        _save_itunes_cache()
                    return art_url
    except Exception:
        pass
    return default_url

def _artwork(uuid: str) -> str:
    """Pocket Casts serves podcast cover art from a stable CDN path.
    Bruno 2026-05-22: the /webp/ path 404s; the working pattern is
    /discover/images/280/<uuid>.jpg."""
    return f"https://static.pocketcasts.com/discover/images/280/{uuid}.jpg" if uuid else ""


def live_status() -> dict:
    email, password = _creds()
    if not (email and password):
        return {"status": "unconfigured",
                "error": "Set pocketcasts.email and pocketcasts.password in Settings."}
    tok = _login()
    if not tok:
        return {"status": "error",
                "error": "Login failed — check Pocket Casts email/password."}
    return {"status": "ok", "source": "pocketcasts_api",
            "note": "Authenticated; podcasts + history available."}


def podcasts() -> list[dict]:
    """Subscribed podcasts, each with title/author/artwork/url."""
    data = _auth_post("/user/podcast/list", {"v": 1})
    if not data:
        return []
    out = []
    for p in data.get("podcasts", []):
        uuid = p.get("uuid", "")
        # lastEpisodePublished drives the "latest" sort Bruno wants.
        last_pub = (p.get("lastEpisodePublished") or "")
        title = p.get("title", "")
        raw_art = _artwork(uuid)
        art_url = _resolve_itunes_artwork(title, raw_art)
        out.append({
            "id":       uuid,
            "title":    title,
            "subtitle": p.get("author", ""),
            "author":   p.get("author", ""),
            "image":    art_url,
            "url":      f"https://pca.st/podcast/{uuid}" if uuid else "",
            "last_published": last_pub[:10],
            "year":     last_pub[:4],
            "meta":     [f"latest {last_pub[:10]}"] if last_pub else [],
        })
    return out


def history(limit: int = 500) -> list[dict]:
    """Recently played episodes."""
    data = _auth_post("/user/history", {})
    if not data:
        return []
    out = []
    for ep in (data.get("episodes") or [])[:limit]:
        puid = ep.get("podcastUuid", "")
        title = ep.get("podcastTitle", "")
        raw_art = _artwork(puid)
        art_url = _resolve_itunes_artwork(title, raw_art)
        out.append({
            "id":       ep.get("uuid", ""),
            "title":    ep.get("title", ""),
            "subtitle": title,
            "image":    art_url,
            "url":      f"https://pca.st/episode/{ep.get('uuid','')}" if ep.get("uuid") else "",
            "year":     (ep.get("published") or "")[:4],
            "meta":     [m for m in [
                f"{round((ep.get('duration') or 0)/60)}m" if ep.get("duration") else "",
            ] if m],
        })
    return out


def snapshot() -> dict:
    ls = live_status()
    if ls.get("status") != "ok":
        return {"status": ls.get("status"), "error": ls.get("error")}
    subs = podcasts()
    hist = history()
    return {
        "status": "ok",
        "synced_at": datetime.now().isoformat(),
        "count": len(subs),
        "items": subs,
        "history": hist,
        "history_count": len(hist),
    }


def items(limit: int = 1000) -> list[dict]:
    return podcasts()[:limit]


def stats() -> dict:
    subs = podcasts()
    return {"status": "ok" if subs else "no-data", "count": len(subs),
            "last_synced": datetime.now().isoformat()[:16]}
