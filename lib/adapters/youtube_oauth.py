"""YouTube OAuth adapter — likes, playlists, subscriptions via the Data API.

Bruno 2026-06-12. Watch HISTORY is API-dead (Google removed it in 2016 —
see lib/youtube_takeout.py + the extension harvest for that). But the API
still serves, with OAuth: liked videos (the LL playlist), the user's
playlists and their items, and subscriptions. This adapter completes the
YouTube picture: Takeout = full history · extension = ongoing history ·
THIS = likes/playlists/subs, refreshed by the daily snapshots unit.

Credentials: reuses the client_id/client_secret already proven in
.gdrive_token.json (same Google Cloud project). Its own token lives in
.youtube_token.json (gitignored by the .*_token.json rule). First run needs
ONE browser consent (authorize()); after that the refresh token keeps it
hands-off forever. If the project lacks the YouTube Data API, calls fail
with a clear 403 → live_status says exactly what to enable.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
GDRIVE_TOKEN = ROOT / ".gdrive_token.json"
TOKEN = ROOT / ".youtube_token.json"
SCOPES = ["https://www.googleapis.com/auth/youtube.readonly"]

META = {
    "id": "youtube_oauth",
    "label": "YouTube (likes/playlists/subs)",
    "icon": "👍",
    "kind": "media",
    "needs_auth": True,
    "destructive_actions": [],
    "read_only_default": True,
}


def _client_conf() -> dict | None:
    try:
        d = json.loads(GDRIVE_TOKEN.read_text(encoding="utf-8"))
        return {"installed": {
            "client_id": d["client_id"],
            "client_secret": d["client_secret"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": d.get("token_uri",
                               "https://oauth2.googleapis.com/token"),
            "redirect_uris": ["http://localhost"],
        }}
    except Exception:
        return None


def _creds():
    """Valid credentials or None (never raises, never prompts)."""
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        if not TOKEN.exists():
            return None
        creds = Credentials.from_authorized_user_info(
            json.loads(TOKEN.read_text(encoding="utf-8")), SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            TOKEN.write_text(creds.to_json(), encoding="utf-8")
        return creds if creds.valid else None
    except Exception:
        return None


def authorize() -> dict:
    """Interactive, ONCE: opens the browser for consent, stores the token."""
    conf = _client_conf()
    if not conf:
        return {"status": "error",
                "error": "no client credentials (.gdrive_token.json missing)"}
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        flow = InstalledAppFlow.from_client_config(conf, SCOPES)
        creds = flow.run_local_server(port=0, open_browser=True,
                                      authorization_prompt_message="")
        TOKEN.write_text(creds.to_json(), encoding="utf-8")
        return {"status": "ok", "token": str(TOKEN)}
    except Exception as e:
        return {"status": "error", "error": str(e)[:200]}


def _yt():
    creds = _creds()
    if not creds:
        return None
    from googleapiclient.discovery import build
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def live_status() -> dict:
    if not TOKEN.exists():
        return {"status": "unconfigured",
                "error": "not authorized yet — run "
                         "lib.adapters.youtube_oauth.authorize() (one browser "
                         "consent; client creds reused from gdrive)"}
    if _creds() is None:
        return {"status": "error", "error": "token invalid/expired beyond refresh"}
    return {"status": "ok", "scopes": "youtube.readonly"}


def _walk_playlist(yt, playlist_id: str, kind: str, cap: int = 5000) -> list[dict]:
    out: list[dict] = []
    page = None
    while len(out) < cap:
        r = yt.playlistItems().list(
            part="snippet,contentDetails", playlistId=playlist_id,
            maxResults=50, pageToken=page).execute()
        for it in r.get("items", []):
            sn = it.get("snippet") or {}
            vid = (it.get("contentDetails") or {}).get("videoId") or ""
            out.append({
                "id": f"yt:{kind}:{vid}",
                "title": (sn.get("title") or "")[:300],
                "url": f"https://www.youtube.com/watch?v={vid}",
                "subtitle": " · ".join(p for p in (
                    sn.get("videoOwnerChannelTitle"),
                    (sn.get("publishedAt") or "")[:10]) if p)[:200],
                "kind": kind,
                "channel": sn.get("videoOwnerChannelTitle") or "",
            })
        page = r.get("nextPageToken")
        if not page:
            break
    return out


def snapshot() -> dict:
    yt = _yt()
    if yt is None:
        return {"status": "unconfigured", "items": [],
                "error": "authorize() first"}
    items: list[dict] = []
    errors: list[str] = []

    try:    # liked videos — the LL playlist still works
        items += _walk_playlist(yt, "LL", "liked_video")
    except Exception as e:
        errors.append(f"likes: {str(e)[:80]}")

    try:    # the user's own playlists + their contents
        page = None
        while True:
            r = yt.playlists().list(part="snippet,contentDetails", mine=True,
                                    maxResults=50, pageToken=page).execute()
            for pl in r.get("items", []):
                sn = pl.get("snippet") or {}
                pid = pl["id"]
                n = (pl.get("contentDetails") or {}).get("itemCount", 0)
                items.append({
                    "id": f"yt:playlist:{pid}",
                    "title": (sn.get("title") or "")[:300],
                    "url": f"https://www.youtube.com/playlist?list={pid}",
                    "subtitle": f"{n} videos"[:200],
                    "kind": "playlist",
                })
                try:
                    items += _walk_playlist(yt, pid, "playlist_video", cap=500)
                except Exception:
                    pass
            page = r.get("nextPageToken")
            if not page:
                break
    except Exception as e:
        errors.append(f"playlists: {str(e)[:80]}")

    try:    # subscriptions
        page = None
        while True:
            r = yt.subscriptions().list(part="snippet", mine=True,
                                        maxResults=50, pageToken=page).execute()
            for sub in r.get("items", []):
                sn = sub.get("snippet") or {}
                cid = (sn.get("resourceId") or {}).get("channelId") or ""
                items.append({
                    "id": f"yt:sub:{cid}",
                    "title": (sn.get("title") or "")[:300],
                    "url": f"https://www.youtube.com/channel/{cid}",
                    "subtitle": (sn.get("description") or "")[:200],
                    "kind": "subscription",
                })
            page = r.get("nextPageToken")
            if not page:
                break
    except Exception as e:
        errors.append(f"subs: {str(e)[:80]}")

    status = "ok" if items else ("error" if errors else "empty")
    out = {"status": status, "synced_at": datetime.now().isoformat(),
           "count": len(items), "items": items}
    if errors:
        out["errors"] = errors
    return out


def items(limit: int = 5000) -> list[dict]:
    return (snapshot().get("items") or [])[:limit]
