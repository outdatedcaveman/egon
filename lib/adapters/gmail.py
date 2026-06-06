"""Gmail — read-only message metadata (no body content fetched by default)."""
from __future__ import annotations

from datetime import datetime

from lib import google_oauth as g
from lib.snapshot_store import latest_snapshot

META = {
    "id": "gmail",
    "label": "Gmail",
    "icon": "📧",
    "kind": "database",
    "needs_auth": True,
    "destructive_actions": [],
    "read_only_default": True,
}

READ_SCOPES  = ["https://www.googleapis.com/auth/gmail.readonly"]
# Write scope = compose, send, modify labels. Opt-in only.
WRITE_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
SCOPES = READ_SCOPES


def _scopes() -> list[str]:
    return g.resolved_scopes("gmail", READ_SCOPES, WRITE_SCOPES)


TOKEN = g.token_path("gmail")


def is_authorized() -> bool: return g.is_authorized(TOKEN, _scopes())
def start_auth_flow() -> dict: return g.start_auth_flow(TOKEN, _scopes(), "gmail")
def revoke() -> dict: return g.revoke(TOKEN)


def live_status() -> dict:
    if not g.client_config("gmail"):
        return {"status": "unconfigured", "error": "Set up Drive first (Gmail reuses its OAuth client)."}
    if not is_authorized():
        return {"status": "unconfigured", "error": "Click Authorize below."}
    is_write = g.mode("gmail") == "readwrite"
    return {"status": "ok", "scopes": _scopes(), "read_only": not is_write,
            "mode": "read+write" if is_write else "read"}


def snapshot() -> dict:
    try:
        svc = g.build_service(TOKEN, _scopes(), "gmail", "v1")
    except Exception as e:
        return {"status": "unconfigured", "error": str(e)}
    try:
        # last 500 messages, INBOX + sent
        items: list[dict] = []
        for label in ("INBOX", "SENT"):
            r = svc.users().messages().list(userId="me", labelIds=[label],
                                             maxResults=250).execute()
            for m in r.get("messages", []):
                detail = svc.users().messages().get(
                    userId="me", id=m["id"], format="metadata",
                    metadataHeaders=["From", "To", "Subject", "Date"],
                ).execute()
                headers = {h["name"]: h["value"]
                           for h in detail.get("payload", {}).get("headers", [])}
                items.append({
                    "id":       detail.get("id"),
                    "title":    headers.get("Subject", "(no subject)")[:200],
                    "from":     headers.get("From", "")[:120],
                    "to":       headers.get("To", "")[:120],
                    "date":     headers.get("Date", ""),
                    "label":    label,
                    "snippet":  detail.get("snippet", "")[:200],
                    "url":      f"https://mail.google.com/mail/u/0/#inbox/{detail.get('id')}",
                })
                if len(items) >= 500: break
            if len(items) >= 500: break
        return {"status": "ok", "synced_at": datetime.now().isoformat(),
                "count": len(items), "items": items}
    except Exception as e:
        return {"status": "error", "error": str(e)[:240]}


def items(limit: int = 100) -> list[dict]:
    s = latest_snapshot(META["id"])
    return s.get("items", [])[:limit] if s and s.get("status") == "ok" else []


def stats() -> dict:
    s = latest_snapshot(META["id"])
    if not s: return {"status": "no-snapshot", "count": 0, "last_synced": None}
    return {"status": s.get("status", "ok"), "count": s.get("count", 0),
            "last_synced": (s.get("synced_at") or "")[:16]}
