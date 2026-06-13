"""TV Time — Playwright-based (their mobile API hardened to non-password auth).

TV Time's old endpoints (api2.tozelabs.com/v2/signin) now reject plain
password posts — they likely hash + sign client-side or use OAuth.
Same fix as Kindle/Paperpile: browser-based login once, saved session, then
headless scrape of app.tvtime.com.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from lib import scraper
from lib.snapshot_store import latest_snapshot

ROOT = Path(__file__).resolve().parent.parent.parent
_LIBRARY_STATE = ROOT / "state" / "panop" / "tvtime_library_state.json"

META = {
    "id": "tvtime",
    "label": "TV Time (login)",
    "icon": "📺",
    "kind": "media",
    "needs_auth": True,
    "destructive_actions": [],
    "read_only_default": True,
}

LOGIN_URL = "https://app.tvtime.com/"


def is_logged_in() -> bool:
    return scraper.is_logged_in("tvtime")


def start_auth_flow() -> dict:
    revoke()
    return scraper.interactive_login(
        "tvtime", LOGIN_URL,
        wait_message="Sign in to TV Time. Close this window when you see your shows.",
        wait_url_contains=None,
        max_wait_seconds=600,
    )


def revoke() -> dict:
    return scraper.revoke("tvtime")


def live_status() -> dict:
    if not is_logged_in():
        return {"status": "unconfigured",
                "error": "Click 'Login to TV Time' to open a browser and sign in once. "
                         "(TV Time's mobile API no longer accepts plain passwords.)"}
    return {"status": "ok", "source": "playwright", "note": "Saved session; sync runs headless."}


def looks_like_entity(d: dict) -> bool:
    return isinstance(d, dict) and ("id" in d or "uuid" in d or "show_id" in d or "entity_id" in d) and ("name" in d or "title" in d)


def walk(obj, collected: list[dict], kind: str = ""):
    if isinstance(obj, dict):
        if looks_like_entity(obj):
            ent = obj.copy()
            if kind:
                ent["_kind"] = kind
            collected.append(ent)
        for k, v in obj.items():
            walk(v, collected, kind or (obj.get("entity_type") if looks_like_entity(obj) else ""))
    elif isinstance(obj, list):
        for v in obj:
            walk(v, collected, kind)


def _resolve_poster(entity: dict) -> str:
    posters = entity.get("posters")
    if isinstance(posters, list) and posters:
        p = posters[0].get("url")
        if p:
            return p
            
    p = (
        entity.get("poster")
        or entity.get("image")
        or entity.get("thumb")
        or entity.get("poster_path")
        or entity.get("artwork")
        or entity.get("image_url")
    )
    if not p and isinstance(entity.get("images"), dict):
        images = entity["images"]
        p = images.get("poster") or images.get("thumb")
        
    if not p:
        return ""
        
    p_str = str(p)
    if p_str.startswith("http://") or p_str.startswith("https://"):
        return p_str
    if p_str.startswith("/"):
        return "https://artworks.thetvdb.com" + p_str
    return p_str


def _snapshot_from_harvest() -> dict | None:
    """Primary source: the Chrome-extension harvest (msapi + Authorization),
    which carries the FULL library — 525 followed series + every watched
    episode rolled up per show. The old Playwright path used the capped
    `only_followed_series` endpoint (20 shows) and needed a headless browser;
    this reads the merge-store the extension keeps current. Bruno 2026-06-13."""
    try:
        st = json.loads(_LIBRARY_STATE.read_text(encoding="utf-8"))
    except Exception:
        return None
    raw = st.get("items") or []
    if not raw:
        return None
    items: list[dict] = []
    for it in raw:
        eid = str(it.get("id") or it.get("tvdb_id") or "")
        if not eid:
            continue
        etype = it.get("entity_type") or it.get("kind") or "series"
        etype = "movie" if "movie" in etype else "series"
        items.append({
            "id": eid,
            "title": str(it.get("title") or "(no title)").strip()[:160],
            "poster": it.get("image") or it.get("poster") or "",
            "url": it.get("url") or f"https://www.thetvdb.com/?id={it.get('tvdb_id','')}",
            "year": str(it.get("year") or "")[:4],
            "status": ("watching" if (it.get("watched_episodes") or 0) > 0
                       else ("following" if it.get("followed") else "")),
            "entity_type": etype,
            "watched_episodes": it.get("watched_episodes") or 0,
            "last_watched": (it.get("last_watched") or "")[:10],
            "rating": str(it.get("rating") or ""),
        })
    items.sort(key=lambda x: (x.get("watched_episodes") or 0), reverse=True)
    return {"status": "ok", "synced_at": st.get("received_at") or datetime.now().isoformat(),
            "count": len(items), "items": items, "source": "extension_harvest"}


def snapshot() -> dict:
    # Prefer the extension harvest (full library). Fall back to the legacy
    # Playwright scrape only if no harvest state exists yet.
    harv = _snapshot_from_harvest()
    if harv:
        return harv
    if not is_logged_in():
        return {"status": "unconfigured", "error": "not logged in"}
    try:
        import httpx
        import base64

        jwt = None
        user_json = None
        captured_api_key = None
        captured_auth_header = None

        def on_request(request):
            nonlocal captured_api_key, captured_auth_header
            if "sidecar" in request.url:
                h = request.headers
                key = h.get("x-api-key")
                authz = h.get("authorization")
                if key:
                    captured_api_key = key
                if authz:
                    captured_auth_header = authz
        
        # Load Playwright to extract localStorage auth details and sniff headers
        with scraper.browser_context("tvtime", headless=True) as ctx:
            page = ctx.new_page()
            page.on("request", on_request)
            # Navigate to welcome page or to-watch page
            page.goto("https://app.tvtime.com/to-watch", wait_until="networkidle", timeout=60_000)
            page.wait_for_timeout(4000)
            jwt = page.evaluate("() => localStorage.getItem('flutter.jwtToken')")
            user_json = page.evaluate("() => localStorage.getItem('flutter.user')")

            if jwt:
                jwt = jwt.strip('"')

            # Fallback to reading window.__egonTvTimeAuth in page context
            if not captured_api_key or not captured_auth_header:
                main_world_auth = page.evaluate("() => window.__egonTvTimeAuth")
                if main_world_auth:
                    if not captured_api_key:
                        captured_api_key = main_world_auth.get("x-api-key")
                    if not captured_auth_header:
                        captured_auth_header = main_world_auth.get("authorization")

        if captured_auth_header:
            captured_auth_header = captured_auth_header.strip('"')

        if not jwt and not captured_auth_header:
            return {"status": "error", "error": "Could not extract auth token from localStorage or page headers. Please log in again."}

        uid = ""
        if user_json:
            try:
                user_data = user_json.strip('"').replace('\\"', '"')
                parsed = json.loads(user_data)
                if isinstance(parsed, str):
                    parsed = json.loads(parsed)
                uid = str(parsed.get("id", ""))
            except Exception:
                pass
        if not uid and jwt:
            try:
                parts = jwt.split('.')
                if len(parts) > 1:
                    p = parts[1]
                    missing_padding = len(p) % 4
                    if missing_padding:
                        p += '=' * (4 - missing_padding)
                    payload = json.loads(base64.b64decode(p).decode('utf-8'))
                    uid = str(payload.get("id", ""))
            except Exception:
                pass

        if not uid and captured_auth_header:
            # Try to decode uid from captured auth header if it has a JWT
            try:
                jwt_part = captured_auth_header.split(" ")[1]
                parts = jwt_part.split('.')
                if len(parts) > 1:
                    p = parts[1]
                    missing_padding = len(p) % 4
                    if missing_padding:
                        p += '=' * (4 - missing_padding)
                    payload = json.loads(base64.b64decode(p).decode('utf-8'))
                    uid = str(payload.get("id", ""))
            except Exception:
                pass

        if not uid:
            return {"status": "error", "error": "Could not determine TV Time user ID."}

        # Build sidecar request endpoints
        def b64_encode(s: str) -> str:
            return base64.b64encode(s.encode('utf-8')).decode('utf-8').rstrip('=')

        def make_sidecar_url(base: str, qs: str) -> str:
            return f"https://app.tvtime.com/sidecar?o_b64={b64_encode(base)}{qs}"

        endpoints = [
            {"label": "followed_series", "base": f"https://msapi.tvtime.com/prod/v1/tracking/cgw/follows/user/{uid}", "qs": "&entity_type=series&filter=only_followed_series"},
            {"label": "followed_movies", "base": f"https://msapi.tvtime.com/prod/v1/tracking/cgw/follows/user/{uid}", "qs": "&entity_type=movie&sort=watched_date,desc"},
            {"label": "watched_movies",  "base": f"https://msapi.tvtime.com/prod/v1/tracking/watches/user/{uid}", "qs": "&entity_type=movie"},
            {"label": "fav_series", "base": f"https://msapi.tvtime.com/prod/v2/lists/user/{uid}/lists/favorite-series", "qs": "&expand=all"},
            {"label": "fav_movies", "base": f"https://msapi.tvtime.com/prod/v2/lists/user/{uid}/lists/favorite-movies", "qs": "&expand=all"},
        ]

        collected_entities: list[dict] = []

        headers = {
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        }
        if captured_api_key:
            headers["x-api-key"] = captured_api_key
        if jwt:
            headers["Authorization"] = f"Bearer {jwt}"
        elif captured_auth_header:
            headers["Authorization"] = captured_auth_header

        for ep in endpoints:
            url = make_sidecar_url(ep["base"], ep["qs"])
            try:
                r = httpx.get(url, headers=headers, timeout=15.0)
                if r.status_code == 200:
                    data = r.json()
                    payload = data.get("data") if isinstance(data, dict) and "data" in data else data
                    walk(payload, collected_entities, ep["label"])
            except Exception:
                pass

        # Deduplicate and format items
        seen_ids = set()
        items: list[dict] = []
        for entity in collected_entities:
            eid = str(entity.get("id") or entity.get("uuid") or entity.get("show_id") or entity.get("entity_id") or "")
            if not eid or eid in seen_ids:
                continue

            if eid == uid or entity.get("login") == uid or entity.get("name") == "Anonymous":
                continue

            seen_ids.add(eid)
            title = entity.get("name") or entity.get("title") or "(no title)"
            poster = _resolve_poster(entity)

            # Determine entity type
            entity_type = entity.get("entity_type") or entity.get("_kind") or ""
            if not entity_type and (entity.get("seasons") is not None or entity.get("number_of_seasons") is not None):
                entity_type = "series"
            if "movie" in entity_type.lower() or "movie" in str(entity.get("_kind") or ""):
                entity_type = "movie"
            elif "series" in entity_type.lower() or "show" in entity_type.lower() or "series" in str(entity.get("_kind") or ""):
                entity_type = "series"

            # Determine URL
            if entity_type == "movie":
                url = f"https://app.tvtime.com/movie/{eid}"
            else:
                url = f"https://app.tvtime.com/show/{eid}"

            # Check year
            year = ""
            if entity.get("year"):
                year = str(entity["year"])
            elif entity.get("first_aired"):
                year = str(entity["first_aired"])[:4]
            elif entity.get("release_date"):
                year = str(entity["release_date"])[:4]

            items.append({
                "id": eid,
                "title": title.strip()[:120],
                "poster": poster,
                "url": url,
                "year": year,
                "status": entity.get("status") or entity.get("watch_status") or "",
                "entity_type": entity_type or "series",
                "rating": str(entity.get("rating") or entity.get("user_rating") or "")
            })

        return {"status": "ok", "synced_at": datetime.now().isoformat(),
                "count": len(items), "items": items}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:240]}"}


def items(limit: int = 100) -> list[dict]:
    snap = latest_snapshot(META["id"])
    return snap.get("items", [])[:limit] if snap and snap.get("status") == "ok" else []


def stats() -> dict:
    snap = latest_snapshot(META["id"])
    if not snap:
        ls = live_status()
        return {"status": ls.get("status", "no-snapshot"), "count": 0, "last_synced": None,
                "error": ls.get("error") or ls.get("note")}
    return {"status": snap.get("status", "ok"), "count": snap.get("count", 0),
            "last_synced": (snap.get("synced_at") or "")[:16]}
