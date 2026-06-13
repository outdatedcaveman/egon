"""Trakt adapter — durable TV/film tracking via a real API.

Bruno 2026-06-13: TV Time is hostile (no export, dead support email, fragile
reverse-engineered API). Trakt has a proper API, auto-scrobbles from
streaming, and imports Letterboxd — so it becomes the durable home for
TV/film, seeded from the TV Time harvest.

Auth = OAuth DEVICE flow (no redirect server, ideal for a desktop app):
  1. authorize() -> prints a code + URL; Bruno visits trakt.tv/activate,
     enters the code, approves.
  2. We poll for the token; refresh token persists in .trakt_token.json
     (gitignored via .*_token.json).

Setup (one time, ~2 min):
  • free account at trakt.tv
  • trakt.tv/oauth/applications -> New Application:
      name: Egon ; redirect uri: urn:ietf:wg:oauth:2.0:oob ; scopes: default
  • copy Client ID + Client Secret into egon-config.json:
      {"trakt": {"client_id": "...", "client_secret": "..."}}
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
CFG = ROOT / "egon-config.json"
TOKEN = ROOT / ".trakt_token.json"
API = "https://api.trakt.tv"

META = {
    "id": "trakt",
    "label": "Trakt (TV & film)",
    "icon": "📺",
    "kind": "media",
    "needs_auth": True,
    "destructive_actions": [],
    "read_only_default": True,
}


def _creds() -> tuple[str, str]:
    try:
        t = json.loads(CFG.read_text(encoding="utf-8")).get("trakt") or {}
        return (t.get("client_id") or "").strip(), (t.get("client_secret") or "").strip()
    except Exception:
        return "", ""


def _httpx():
    from lib.lazy_httpx import httpx
    return httpx


def _token() -> str | None:
    try:
        d = json.loads(TOKEN.read_text(encoding="utf-8"))
    except Exception:
        return None
    # refresh if near expiry
    if d.get("created_at", 0) + d.get("expires_in", 0) - 86400 < time.time():
        cid, csec = _creds()
        try:
            r = _httpx().post(f"{API}/oauth/token", timeout=20, json={
                "refresh_token": d.get("refresh_token"),
                "client_id": cid, "client_secret": csec,
                "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
                "grant_type": "refresh_token"})
            if r.status_code == 200:
                d = r.json()
                TOKEN.write_text(json.dumps(d), encoding="utf-8")
        except Exception:
            pass
    return d.get("access_token")


def _headers(auth: bool = True) -> dict:
    cid, _ = _creds()
    h = {"Content-Type": "application/json", "trakt-api-version": "2",
         "trakt-api-key": cid}
    if auth:
        tok = _token()
        if tok:
            h["Authorization"] = f"Bearer {tok}"
    return h


def authorize(print_fn=print) -> dict:
    """Device-flow: returns the code/URL for Bruno, then polls for the token."""
    cid, csec = _creds()
    if not cid or not csec:
        return {"status": "error",
                "error": "set trakt.client_id + client_secret in egon-config.json"}
    httpx = _httpx()
    r = httpx.post(f"{API}/oauth/device/code", json={"client_id": cid}, timeout=20)
    if r.status_code != 200:
        return {"status": "error", "error": f"device/code HTTP {r.status_code}"}
    dc = r.json()
    print_fn(f"\n  → Go to {dc['verification_url']} and enter code: "
             f"{dc['user_code']}\n    (waiting up to {dc['expires_in']}s)…")
    deadline = time.time() + dc["expires_in"]
    interval = dc.get("interval", 5)
    while time.time() < deadline:
        time.sleep(interval)
        tr = httpx.post(f"{API}/oauth/device/token", timeout=20, json={
            "code": dc["device_code"], "client_id": cid, "client_secret": csec})
        if tr.status_code == 200:
            TOKEN.write_text(json.dumps(tr.json()), encoding="utf-8")
            return {"status": "ok", "token": str(TOKEN)}
        if tr.status_code == 400:
            continue          # authorization pending
        if tr.status_code in (404, 409, 410, 418):
            return {"status": "error", "error": f"device flow ended HTTP {tr.status_code}"}
    return {"status": "error", "error": "timed out waiting for approval"}


def live_status() -> dict:
    cid, csec = _creds()
    if not cid or not csec:
        return {"status": "unconfigured",
                "error": "add trakt.client_id + client_secret to egon-config.json "
                         "(trakt.tv/oauth/applications)"}
    if not TOKEN.exists():
        return {"status": "unconfigured",
                "error": "authorize once: lib.adapters.trakt.authorize()"}
    if not _token():
        return {"status": "error", "error": "token invalid; re-authorize"}
    return {"status": "ok"}


def _get(path: str) -> list | dict | None:
    try:
        r = _httpx().get(f"{API}{path}", headers=_headers(), timeout=30)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def snapshot() -> dict:
    if not _token():
        return {"status": "unconfigured", "items": []}
    items: list[dict] = []

    for show in (_get("/sync/watched/shows") or []):
        s = show.get("show", {})
        ids = s.get("ids", {})
        items.append({
            "id": f"trakt:show:{ids.get('trakt')}",
            "title": s.get("title", ""), "year": str(s.get("year") or ""),
            "kind": "watched_show",
            "subtitle": f"{show.get('plays', 0)} plays · last "
                        f"{(show.get('last_watched_at') or '')[:10]}",
            "url": f"https://trakt.tv/shows/{ids.get('slug','')}",
            "when": (show.get("last_watched_at") or "")[:19],
        })
    for mv in (_get("/sync/watched/movies") or []):
        m = mv.get("movie", {}); ids = m.get("ids", {})
        items.append({
            "id": f"trakt:movie:{ids.get('trakt')}",
            "title": m.get("title", ""), "year": str(m.get("year") or ""),
            "kind": "watched_movie",
            "subtitle": f"last {(mv.get('last_watched_at') or '')[:10]}",
            "url": f"https://trakt.tv/movies/{ids.get('slug','')}",
            "when": (mv.get("last_watched_at") or "")[:19],
        })
    for rt in (_get("/sync/ratings") or []):
        obj = rt.get(rt.get("type", ""), {})
        if not obj:
            continue
        items.append({
            "id": f"trakt:rating:{rt.get('type')}:{obj.get('ids',{}).get('trakt')}",
            "title": obj.get("title", ""), "kind": f"rated_{rt.get('type')}",
            "subtitle": f"★ {rt.get('rating')}/10",
            "rating": rt.get("rating"),
            "when": (rt.get("rated_at") or "")[:19],
        })
    return {"status": "ok" if items else "empty",
            "synced_at": datetime.now().isoformat(),
            "count": len(items), "items": items}


def items(limit: int = 5000) -> list[dict]:
    return (snapshot().get("items") or [])[:limit]


# ── TV Time → Trakt bridge ───────────────────────────────────────────────────
def push_tvtime_history() -> dict:
    """Push the TV Time harvest (watched shows/episodes/movies) into Trakt via
    /sync/history. Matches by title+year -> Trakt search. Best-effort; Trakt
    dedups its own history, so re-runs are safe."""
    if not _token():
        return {"status": "unconfigured"}
    try:
        st = json.loads((ROOT / "state" / "panop"
                         / "tvtime_library_state.json").read_text(encoding="utf-8"))
    except Exception:
        return {"status": "no_tvtime_data"}
    httpx = _httpx()
    shows, movies, added, miss = [], [], 0, 0
    for it in (st.get("items") or []):
        title = it.get("title") or ""
        if not title:
            continue
        typ = "show" if "series" in str(it.get("entity_type", "")).lower() \
            or it.get("entity_type") in ("series", "watched_episode") else "movie"
        try:
            sr = httpx.get(f"{API}/search/{typ}", headers=_headers(),
                           params={"query": title, "limit": 1}, timeout=20)
            hit = (sr.json() or [{}])[0] if sr.status_code == 200 else {}
            ids = (hit.get(typ) or {}).get("ids")
            if ids:
                (shows if typ == "show" else movies).append({"ids": ids})
                added += 1
            else:
                miss += 1
        except Exception:
            miss += 1
        time.sleep(0.2)
    if shows or movies:
        httpx.post(f"{API}/sync/history", headers=_headers(),
                   json={"shows": shows, "movies": movies}, timeout=40)
    return {"status": "ok", "matched": added, "unmatched": miss}
