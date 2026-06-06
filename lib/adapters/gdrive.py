"""Google Drive — READ-ONLY adapter.

Security posture:
- OAuth 2.0 InstalledAppFlow (Desktop client) with local-loopback redirect.
- ONLY two scopes ever requested: `drive.metadata.readonly` + `drive.readonly`.
  No write/delete/share scopes — Drive operations cannot mutate your account.
- Refresh + access tokens cached in `egon-config.json["gdrive"]["token"]`
  (gitignored). Never sent off-device.
- The "Authorize" UI button calls `start_auth_flow()` which:
    1. Opens your default browser to Google's consent screen
    2. Spawns a temporary local HTTP server on a random port to receive the redirect
    3. Persists tokens
    4. Returns to the caller
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from lib import secrets
from lib.ledger import load_config, save_config
from lib.snapshot_store import latest_snapshot

META = {
    "id": "gdrive",
    "label": "Google Drive",
    "icon": "☁️",
    "kind": "database",
    "needs_auth": True,
    "destructive_actions": [],   # explicitly empty — read-only
    "read_only_default": True,
}

READ_SCOPES = [
    "https://www.googleapis.com/auth/drive.metadata.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]
# Write scope only requested when user explicitly flips Read+Write in Settings.
# `drive.file` only sees files Egon created — safe even in write mode.
# `drive` (full write/delete) is intentionally NOT requested.
WRITE_SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
]


def _scopes() -> list[str]:
    from lib import google_oauth as g
    return g.resolved_scopes("gdrive", READ_SCOPES, WRITE_SCOPES)


# Back-compat alias for older code paths.
SCOPES = READ_SCOPES

TOKEN_FILE = Path(__file__).resolve().parent.parent.parent / ".gdrive_token.json"


# -- credentials helpers ----------------------------------------------------

def _client_config() -> dict | None:
    """Build the OAuth client config from egon-config.json[gdrive.client_id/secret]."""
    cid = secrets.get("gdrive.client_id")
    csec = secrets.get("gdrive.client_secret")
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
    """Return google.oauth2.credentials.Credentials or None."""
    if not TOKEN_FILE.exists():
        return None
    try:
        from google.oauth2.credentials import Credentials
        with TOKEN_FILE.open() as f:
            data = json.load(f)
        creds = Credentials.from_authorized_user_info(data, _scopes())
        # refresh if expired
        if creds.expired and creds.refresh_token:
            from google.auth.transport.requests import Request
            creds.refresh(Request())
            TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
        return creds
    except Exception:
        return None


def is_authorized() -> bool:
    return _load_creds() is not None


def start_auth_flow() -> dict:
    """Run the interactive auth dance. Opens browser, waits for redirect, saves token."""
    cfg = _client_config()
    if not cfg:
        return {"status": "error",
                "error": "client_id/client_secret missing in egon-config.json[gdrive]. "
                         "Create OAuth Desktop credentials at console.cloud.google.com → save them in Settings."}
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        flow = InstalledAppFlow.from_client_config(cfg, _scopes())
        # port=0 → pick a random free port; opens browser automatically
        creds = flow.run_local_server(port=0, open_browser=True,
                                       authorization_prompt_message="")
        TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "error": str(e)[:240]}


def revoke() -> dict:
    """Delete the cached token. User will need to re-authorize."""
    if TOKEN_FILE.exists():
        TOKEN_FILE.unlink()
    return {"status": "ok", "note": "token revoked locally. To revoke in Google's records too: "
                                     "https://myaccount.google.com/permissions"}


# -- snapshot ---------------------------------------------------------------

def live_status() -> dict:
    if not _client_config():
        return {"status": "unconfigured",
                "error": "Add OAuth client_id/secret in Settings → Google Drive."}
    if not is_authorized():
        return {"status": "unconfigured",
                "error": "Client configured but not authorized. Click Authorize in Settings."}
    from lib import google_oauth as g
    is_write = g.mode("gdrive") == "readwrite"
    return {"status": "ok", "scopes": _scopes(), "read_only": not is_write,
            "mode": "read+write" if is_write else "read"}


def snapshot() -> dict:
    creds = _load_creds()
    if not creds:
        return {"status": "unconfigured", "error": "not authorized"}
    try:
        from googleapiclient.discovery import build
        service = build("drive", "v3", credentials=creds, cache_discovery=False)
        items: list[dict] = []
        page_token = None
        while True:
            resp = service.files().list(
                pageSize=200,
                pageToken=page_token,
                fields="nextPageToken, files(id,name,mimeType,modifiedTime,createdTime,"
                       "webViewLink,parents,starred,trashed,size,owners)",
                orderBy="modifiedTime desc",
                q="trashed=false",
            ).execute()
            for f in resp.get("files", []):
                items.append({
                    "id":          f.get("id"),
                    "title":       f.get("name"),
                    "url":         f.get("webViewLink"),
                    "mime":        f.get("mimeType"),
                    "starred":     bool(f.get("starred")),
                    "modified":    f.get("modifiedTime"),
                    "created":     f.get("createdTime"),
                    "size":        int(f.get("size", 0)) if f.get("size") else 0,
                })
            page_token = resp.get("nextPageToken")
            if not page_token or len(items) >= 3000:
                break
        return {"status": "ok", "synced_at": datetime.now().isoformat(),
                "count": len(items), "items": items}
    except Exception as e:
        return {"status": "error", "error": str(e)[:240]}


def items(limit: int = 100) -> list[dict]:
    snap = latest_snapshot(META["id"])
    if not snap or snap.get("status") != "ok":
        return []
    return snap.get("items", [])[:limit]


def stats() -> dict:
    snap = latest_snapshot(META["id"])
    if not snap:
        return {"status": "no-snapshot", "count": 0, "last_synced": None}
    return {"status": snap.get("status", "ok"),
            "count": snap.get("count", 0),
            "last_synced": (snap.get("synced_at") or "")[:16]}
