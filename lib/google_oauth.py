"""Shared Google OAuth helper — every Google adapter uses this.

One OAuth client (the user's Drive one) covers all Google services. Each
adapter declares its own SCOPES + TOKEN_FILE and reuses this module's helpers.

All scopes are read-only by convention; Egon never requests write scopes.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from lib import secrets

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def mode(service_id: str) -> str:
    """Returns 'read' or 'readwrite' for this Google service. Defaults to 'read'."""
    return secrets.get(f"{service_id}.mode") or "read"


def resolved_scopes(service_id: str, read_scopes: list[str],
                    write_scopes: list[str]) -> list[str]:
    """Pick scopes based on the user's opt-in mode. Always at least read."""
    if mode(service_id) == "readwrite":
        return list(set(read_scopes + write_scopes))
    return list(read_scopes)


def client_config(service_id: str | None = None) -> dict | None:
    """Build Google OAuth client config. Prefer per-service client_id/secret;
    fall back to the shared Drive client (since most users only set up one)."""
    cid = None
    csec = None
    if service_id:
        cid = secrets.get(f"{service_id}.client_id")
        csec = secrets.get(f"{service_id}.client_secret")
    cid = cid or secrets.get("gdrive.client_id")
    csec = csec or secrets.get("gdrive.client_secret")
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


def load_creds(token_path: Path, scopes: list[str]):
    """Load Credentials from disk, refreshing if expired. Returns None if absent/invalid."""
    if not token_path.exists():
        return None
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        data = json.loads(token_path.read_text(encoding="utf-8"))
        creds = Credentials.from_authorized_user_info(data, scopes)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            token_path.write_text(creds.to_json(), encoding="utf-8")
        return creds
    except Exception:
        return None


def is_authorized(token_path: Path, scopes: list[str]) -> bool:
    return load_creds(token_path, scopes) is not None


def start_auth_flow(token_path: Path, scopes: list[str],
                    service_id: str | None = None) -> dict:
    """Run InstalledAppFlow with a local-loopback redirect. Saves token to disk."""
    cfg = client_config(service_id)
    if not cfg:
        return {"status": "error",
                "error": "No OAuth client (set up Drive first, or paste client_id/secret for this service)."}
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        flow = InstalledAppFlow.from_client_config(cfg, scopes)
        creds = flow.run_local_server(port=0, open_browser=True,
                                       authorization_prompt_message="")
        token_path.write_text(creds.to_json(), encoding="utf-8")
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "error": str(e)[:240]}


def revoke(token_path: Path) -> dict:
    if token_path.exists():
        token_path.unlink()
    return {"status": "ok"}


def build_service(token_path: Path, scopes: list[str], api_name: str, version: str):
    """Return a discovery-built Google service, or raise if no creds."""
    import httplib2
    import google_auth_httplib2
    from googleapiclient.discovery import build
    creds = load_creds(token_path, scopes)
    if not creds:
        raise RuntimeError("not authorized")
    http = google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=30))
    return build(api_name, version, http=http, cache_discovery=False)


# common token paths
def token_path(name: str) -> Path:
    return PROJECT_ROOT / f".{name}_token.json"
