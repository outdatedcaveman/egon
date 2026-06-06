"""Instapaper Full API — OAuth1 / xAuth.

To enable:
1. Email api@instapaper.com requesting an OAuth consumer key+secret.
   (Approval can take a few days.)
2. Once received, add to egon-config.json:
     "instapaper_full": {
       "consumer_key":    "...",
       "consumer_secret": "..."
     }
3. The first call to `snapshot()` runs xAuth: exchanges your Simple-API
   username/password for an oauth_token + token_secret, cached on disk.
4. After that, list/star/archive/folders all work via signed requests.

Security: the user's password is sent ONCE during xAuth, never stored.
oauth_token+token_secret are stored in `egon-config.json["instapaper_full"]["token"]`
and `["token_secret"]` — local-only, gitignored.
"""
from __future__ import annotations

from datetime import datetime

import httpx
from requests_oauthlib import OAuth1

from lib import secrets
from lib.ledger import load_config, save_config
from lib.snapshot_store import latest_snapshot

META = {
    "id": "instapaper_full",
    "label": "Instapaper Reading List",
    "icon": "📥",
    "kind": "artifact",
    "needs_auth": True,
    "destructive_actions": ["archive", "delete", "move"],
    "read_only_default": True,
}

ACCESS_TOKEN_URL = "https://www.instapaper.com/api/1/oauth/access_token"
LIST_URL         = "https://www.instapaper.com/api/1.1/bookmarks/list"
ARCHIVE_URL      = "https://www.instapaper.com/api/1.1/bookmarks/archive"
STAR_URL         = "https://www.instapaper.com/api/1.1/bookmarks/star"
FOLDERS_URL      = "https://www.instapaper.com/api/1.1/folders/list"


def _consumer() -> tuple[str | None, str | None]:
    return (secrets.get("instapaper_full.consumer_key"),
            secrets.get("instapaper_full.consumer_secret"))


def _token() -> tuple[str | None, str | None]:
    return (secrets.get("instapaper_full.token"),
            secrets.get("instapaper_full.token_secret"))


def _xauth() -> dict:
    """Exchange username/password for an OAuth1 access token. Run once per user."""
    ck, cs = _consumer()
    if not ck or not cs:
        return {"status": "error", "error": "consumer_key/secret missing — email api@instapaper.com"}
    user = secrets.get("instapaper.username")
    pwd  = secrets.get("instapaper.password")
    if not user or not pwd:
        return {"status": "error", "error": "set instapaper.username + instapaper.password (Simple API creds re-used for xAuth)"}
    auth = OAuth1(ck, cs)
    try:
        r = httpx.post(
            ACCESS_TOKEN_URL,
            data={
                "x_auth_username": user,
                "x_auth_password": pwd,
                "x_auth_mode":     "client_auth",
            },
            auth=auth,                   # type: ignore[arg-type]
            timeout=15.0,
        )
    except Exception as e:
        return {"status": "error", "error": str(e)}
    if r.status_code != 200:
        return {"status": "error", "error": f"HTTP {r.status_code}: {r.text[:200]}"}
    # response is form-encoded: oauth_token=...&oauth_token_secret=...
    pairs = dict(p.split("=", 1) for p in r.text.split("&") if "=" in p)
    tok, secret = pairs.get("oauth_token"), pairs.get("oauth_token_secret")
    if not tok or not secret:
        return {"status": "error", "error": "no token in response"}
    cfg = load_config()
    cfg.setdefault("instapaper_full", {})
    cfg["instapaper_full"]["token"] = tok
    cfg["instapaper_full"]["token_secret"] = secret
    save_config(cfg)
    return {"status": "ok"}


def _signed_post(url: str, data: dict) -> httpx.Response | None:
    ck, cs = _consumer()
    tok, ts = _token()
    if not (ck and cs and tok and ts):
        return None
    auth = OAuth1(ck, cs, tok, ts)
    return httpx.post(url, data=data, auth=auth, timeout=20.0)  # type: ignore[arg-type]


def live_status() -> dict:
    ck, _ = _consumer()
    tok, _ = _token()
    if not ck:
        return {"status": "unconfigured",
                "error": "email api@instapaper.com for consumer_key — see lib/adapters/instapaper_full.py docstring"}
    if not tok:
        return {"status": "unconfigured",
                "error": "consumer key set but no access token — click Sync now to run xAuth"}
    return {"status": "ok"}


def snapshot() -> dict:
    if not _token()[0]:
        result = _xauth()
        if result.get("status") != "ok":
            return {"status": "unconfigured", "error": result.get("error", "xauth failed")}
    try:
        r = _signed_post(LIST_URL, {"limit": "500"})
        if not r or r.status_code != 200:
            return {"status": "error", "error": f"HTTP {r.status_code if r else 'no-response'}: {(r.text if r else '')[:200]}"}
        data = r.json()
    except Exception as e:
        return {"status": "error", "error": str(e)}

    # response is a list mixing user, bookmark, meta blocks
    bookmarks = [b for b in data if b.get("type") == "bookmark"]
    items = [{
        "id":      b.get("bookmark_id"),
        "title":   b.get("title", ""),
        "url":     b.get("url", ""),
        "time":    b.get("time"),
        "starred": b.get("starred") == "1",
        "progress": float(b.get("progress", 0) or 0),
        "description": (b.get("description") or "")[:200],
    } for b in bookmarks]

    return {
        "status": "ok",
        "synced_at": datetime.now().isoformat(),
        "count": len(items),
        "items": items,
    }


def items(limit: int = 100) -> list[dict]:
    snap = latest_snapshot(META["id"])
    if not snap or snap.get("status") != "ok":
        return []
    return snap.get("items", [])[:limit]


def stats() -> dict:
    snap = latest_snapshot(META["id"])
    if not snap:
        return {"status": "no-snapshot", "count": 0, "last_synced": None,
                "error": "click Sync now (will run xAuth if needed)"}
    return {
        "status": snap.get("status", "ok"),
        "count": snap.get("count", 0),
        "last_synced": (snap.get("synced_at") or "")[:16],
    }
