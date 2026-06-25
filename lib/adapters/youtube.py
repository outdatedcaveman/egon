"""YouTube + YouTube Music — REAL-TIME read-only API access.

YouTube Music data lives inside the same Google account as your YouTube account.
Liking a song in YT Music → it's a video like in YouTube Data API. So one
OAuth grant pulls everything live: liked songs/videos, playlists, subscriptions.

Reuses your existing Google Drive OAuth client (same client_id/secret). You
authorize ONCE per Google scope — the consent screen shows both Drive + YouTube
permissions, you click allow, both adapters work forever.

Scope is strictly read-only (`youtube.readonly`). Egon cannot like, unlike,
subscribe, unsubscribe, or modify anything in your YouTube account.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from lib import secrets
from lib.snapshot_store import latest_snapshot

META = {
    "id": "youtube_music",
    "label": "YouTube + YouTube Music",
    "icon": "🎵",
    "kind": "media",
    "needs_auth": True,
    "destructive_actions": [],   # read-only
    "read_only_default": True,
}

READ_SCOPES  = ["https://www.googleapis.com/auth/youtube.readonly"]
# Write scope = full YouTube management (create/delete playlists, subscribe, like).
# Opt-in only via Settings → YouTube → Read+Write toggle.
WRITE_SCOPES = ["https://www.googleapis.com/auth/youtube"]


def _scopes() -> list[str]:
    from lib import google_oauth as g
    return g.resolved_scopes("youtube_music", READ_SCOPES, WRITE_SCOPES)


SCOPES = READ_SCOPES  # back-compat
TOKEN_FILE = Path(__file__).resolve().parent.parent.parent / ".youtube_token.json"


def _client_config() -> dict | None:
    """Reuse Drive's OAuth client by default; fall back to dedicated youtube_music.* keys."""
    cid = secrets.get("youtube_music.client_id") or secrets.get("gdrive.client_id")
    csec = secrets.get("youtube_music.client_secret") or secrets.get("gdrive.client_secret")
    if not cid or not csec:
        return None
    return {
        "installed": {
            "client_id":     cid,
            "client_secret": csec,
            "auth_uri":      "https://accounts.google.com/o/oauth2/auth",
            "token_uri":     "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://127.0.0.1"],
        }
    }


def _load_creds():
    if not TOKEN_FILE.exists():
        return None
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        with TOKEN_FILE.open() as f:
            data = json.load(f)
        creds = Credentials.from_authorized_user_info(data, _scopes())
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
        return creds
    except Exception:
        return None


def is_authorized() -> bool:
    return _load_creds() is not None


def start_auth_flow() -> dict:
    cfg = _client_config()
    if not cfg:
        return {"status": "error",
                "error": "No OAuth client. Either set up Google Drive first (YouTube reuses it) "
                         "or paste your own client_id/secret in this adapter's settings."}
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        flow = InstalledAppFlow.from_client_config(cfg, _scopes())
        creds = flow.run_local_server(port=0, open_browser=True,
                                       authorization_prompt_message="")
        TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "error": str(e)[:240]}


def revoke() -> dict:
    if TOKEN_FILE.exists():
        TOKEN_FILE.unlink()
    return {"status": "ok"}


def live_status() -> dict:
    if not _client_config():
        return {"status": "unconfigured",
                "error": "Set up Google Drive first (YouTube reuses its OAuth client), "
                         "or paste a separate youtube_music.client_id/secret."}
    if not is_authorized():
        return {"status": "unconfigured",
                "error": "OAuth client configured but not authorized. Click Authorize below."}
    from lib import google_oauth as g
    is_write = g.mode("youtube_music") == "readwrite"
    return {"status": "ok", "scopes": _scopes(), "read_only": not is_write,
            "mode": "read+write" if is_write else "read"}


def _service():
    import httplib2
    import google_auth_httplib2
    from googleapiclient.discovery import build
    creds = _load_creds()
    if creds:
        http = google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=30))
    else:
        http = httplib2.Http(timeout=30)
    return build("youtube", "v3", http=http, cache_discovery=False)


def snapshot() -> dict:
    creds = _load_creds()
    if not creds:
        return {"status": "unconfigured", "error": "not authorized"}
    try:
        svc = _service()
        items: list[dict] = []

        # 1) Liked videos via the auto-playlist "LL" (NOT videos.list?myRating
        #    =like, which the API HARD-CAPS at 1000). The LL playlist paginates
        #    without that cap, so we get all 4000+. Bruno 2026-05-22.
        #    categoryId isn't on playlistItems, so we batch-fetch it from
        #    videos.list for the music/non-music split.
        page_token = None
        liked_raw = []          # (videoId, title, channel, published, thumb)
        while True:
            r = svc.playlistItems().list(
                part="snippet,contentDetails", playlistId="LL",
                maxResults=50, pageToken=page_token,
            ).execute()
            for it in r.get("items", []):
                sn = it.get("snippet", {})
                vid = (it.get("contentDetails") or {}).get("videoId") \
                      or (sn.get("resourceId") or {}).get("videoId", "")
                if not vid:
                    continue
                liked_raw.append({
                    "id": vid,
                    "title": sn.get("title", ""),
                    "channel": sn.get("videoOwnerChannelTitle", "") or sn.get("channelTitle", ""),
                    "published": sn.get("publishedAt", ""),
                    "thumbnail": (sn.get("thumbnails", {}).get("high", {}) or
                                  sn.get("thumbnails", {}).get("default", {})).get("url", ""),
                })
            page_token = r.get("nextPageToken")
            if not page_token or len(liked_raw) >= 100000:
                break

        # Batch-fetch categoryId + stats (50 ids/call) for the music split.
        cat_by_id = {}
        ids = [x["id"] for x in liked_raw]
        for i in range(0, len(ids), 50):
            try:
                rr = svc.videos().list(part="snippet,statistics",
                                       id=",".join(ids[i:i + 50])).execute()
                for v in rr.get("items", []):
                    cat_by_id[v["id"]] = {
                        "cat": (v.get("snippet") or {}).get("categoryId", ""),
                        "views": int((v.get("statistics") or {}).get("viewCount", 0) or 0),
                    }
            except Exception:
                pass
        for x in liked_raw:
            meta = cat_by_id.get(x["id"], {})
            cat = meta.get("cat", "")
            channel = x["channel"]
            is_music = (cat == "10") or channel.endswith("- Topic")
            items.append({
                "type": "liked", "id": x["id"], "title": x["title"],
                "channel": channel, "published": x["published"],
                "categoryId": cat, "is_music": is_music,
                "thumbnail": x["thumbnail"],
                "url": f"https://www.youtube.com/watch?v={x['id']}",
                "liked": True, "views": meta.get("views", 0),
            })

        # 2) Own playlists (incl. YT Music auto-generated ones if user has any)
        playlists = []
        page_token = None
        while True:
            r = svc.playlists().list(part="snippet,contentDetails",
                                     mine=True, maxResults=50,
                                     pageToken=page_token).execute()
            for pl in r.get("items", []):
                sn = pl.get("snippet", {})
                playlists.append({
                    "id":          pl.get("id"),
                    "title":       sn.get("title", ""),
                    "description": sn.get("description", "")[:200],
                    "count":       pl.get("contentDetails", {}).get("itemCount", 0),
                    "thumbnail":   (sn.get("thumbnails", {}).get("high", {}) or
                                    sn.get("thumbnails", {}).get("default", {})).get("url", ""),
                    "url":         f"https://www.youtube.com/playlist?list={pl.get('id')}",
                    "published":   sn.get("publishedAt", ""),
                })
            page_token = r.get("nextPageToken")
            if not page_token:
                break

        # 3) Subscriptions
        subs = []
        page_token = None
        while True:
            r = svc.subscriptions().list(part="snippet",
                                          mine=True, maxResults=50,
                                          pageToken=page_token).execute()
            for s in r.get("items", []):
                sn = s.get("snippet", {})
                subs.append({
                    "channel":   sn.get("title", ""),
                    "channelId": sn.get("resourceId", {}).get("channelId", ""),
                    "thumbnail": (sn.get("thumbnails", {}).get("high", {}) or
                                  sn.get("thumbnails", {}).get("default", {})).get("url", ""),
                    "published": sn.get("publishedAt", ""),
                })
            page_token = r.get("nextPageToken")
            if not page_token or len(subs) >= 5000:   # was 1000
                break

        # 4) Playlist CONTENTS — fetch items so they are indexed in Connect and shown in UI.
        for pl in playlists:
            pl["tracks"] = []
            try:
                pid = pl["id"]
                page_token_pl = None
                while True:
                    pr = svc.playlistItems().list(
                        part="snippet,contentDetails", playlistId=pid,
                        maxResults=50, pageToken=page_token_pl).execute()
                    for it in pr.get("items", []):
                        sn = it.get("snippet", {})
                        vid = (it.get("contentDetails") or {}).get("videoId") \
                              or (sn.get("resourceId") or {}).get("videoId", "")
                        if not vid:
                            continue
                        track_item = {
                            "id": vid,
                            "title": sn.get("title", ""),
                            "channel": sn.get("videoOwnerChannelTitle", "") or sn.get("channelTitle", ""),
                            "published": sn.get("publishedAt", ""),
                            "thumbnail": (sn.get("thumbnails", {}).get("high", {}) or
                                          sn.get("thumbnails", {}).get("default", {})).get("url", ""),
                            "url": f"https://www.youtube.com/watch?v={vid}",
                        }
                        pl["tracks"].append(track_item)
                        
                        # Append to items list with type="playlist_video" so they are searchable/indexed
                        is_music = "music" in pl["title"].lower() or track_item["channel"].endswith("- Topic")
                        items.append({
                            "type": "playlist_video",
                            "id": vid,
                            "title": track_item["title"],
                            "channel": track_item["channel"],
                            "published": track_item["published"],
                            "is_music": is_music,
                            "thumbnail": track_item["thumbnail"],
                            "url": track_item["url"],
                            "playlist_id": pid,
                            "playlist_title": pl["title"]
                        })
                    page_token_pl = pr.get("nextPageToken")
                    if not page_token_pl:
                        break
            except Exception:
                pass

        return {
            "status": "ok",
            "synced_at": datetime.now().isoformat(),
            "count": len(items),
            "items": items,
            "playlists": playlists,
            "subscriptions": subs,
        }
    except Exception as e:
        return {"status": "error", "error": str(e)[:300]}


# In-process cache so the Media page can call live without hammering the API.
# YouTube's liked-videos walk can take 10-30s, so we cache for 30 min and
# refresh in the background. Bruno 2026-05-21: items() used to read an empty
# snapshot store; now it fetches live (cached).
import threading as _threading
import time as _time
_YT_CACHE = {"snap": None, "ts": 0.0, "lock": _threading.Lock(), "refreshing": False}
_YT_TTL_S = 30 * 60


def _cached_snapshot(force: bool = False) -> dict:
    now = _time.time()
    with _YT_CACHE["lock"]:
        if _YT_CACHE["snap"] is None:
            try:
                from lib import snapshot_store
                disk_snap = snapshot_store.latest_snapshot("youtube_music")
                if disk_snap:
                    _YT_CACHE["snap"] = disk_snap
                    # Set the cache timestamp so we trigger a background refresh in 60s
                    _YT_CACHE["ts"] = now - _YT_TTL_S + 60
            except Exception:
                pass

        fresh = _YT_CACHE["snap"] and (now - _YT_CACHE["ts"] < _YT_TTL_S)
        if fresh and not force:
            return _YT_CACHE["snap"]
        if _YT_CACHE["refreshing"]:
            return _YT_CACHE["snap"] or {"status": "warming", "items": []}
        _YT_CACHE["refreshing"] = True

    def _bg():
        try:
            s = snapshot()
            if s.get("status") == "ok":
                with _YT_CACHE["lock"]:
                    _YT_CACHE["snap"] = s
                    _YT_CACHE["ts"] = _time.time()
        finally:
            with _YT_CACHE["lock"]:
                _YT_CACHE["refreshing"] = False

    # If we STILL have no cache (e.g. no disk snapshot exists), run synchronously
    if _YT_CACHE["snap"] is None:
        _bg()
        return _YT_CACHE["snap"] or {"status": "warming", "items": []}
    _threading.Thread(target=_bg, daemon=True, name="youtube-refresh").start()
    return _YT_CACHE["snap"]


def items(limit: int = 100000) -> list[dict]:
    """All liked items (videos + music)."""
    snap = _cached_snapshot()
    return (snap.get("items") or [])[:limit] if snap else []


def videos(limit: int = 100000) -> list[dict]:
    """Liked YouTube videos (non-music)."""
    return [v for v in items(100000) if not v.get("is_music")][:limit]


def music(limit: int = 100000) -> list[dict]:
    """Liked YouTube Music tracks (Music category or '- Topic' channels)."""
    return [v for v in items(100000) if v.get("is_music")][:limit]


def subscription_items(limit: int = 5000) -> list[dict]:
    snap = _cached_snapshot()
    return (snap.get("subscriptions") or [])[:limit] if snap else []


def playlists() -> list[dict]:
    """Your playlists (with .tracks contents). Reads the live cache so it
    matches items()/music() rather than a stale snapshot-store file."""
    snap = _cached_snapshot()
    return (snap.get("playlists") or []) if snap else []


def stats() -> dict:
    snap = latest_snapshot(META["id"])
    if not snap:
        return {"status": "no-snapshot", "count": 0, "last_synced": None}
    return {"status": snap.get("status", "ok"),
            "count": snap.get("count", 0),
            "last_synced": (snap.get("synced_at") or "")[:16]}
