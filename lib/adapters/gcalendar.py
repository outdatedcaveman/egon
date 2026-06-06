"""Google Calendar — read-only events from all your calendars."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from lib import google_oauth as g
from lib.snapshot_store import latest_snapshot

META = {
    "id": "gcalendar",
    "label": "Google Calendar",
    "icon": "📅",
    "kind": "database",
    "needs_auth": True,
    "destructive_actions": [],
    "read_only_default": True,
}

READ_SCOPES  = ["https://www.googleapis.com/auth/calendar.readonly"]
# Write scope = create/modify/delete events.
WRITE_SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
SCOPES = READ_SCOPES  # back-compat


def _scopes() -> list[str]:
    return g.resolved_scopes("gcalendar", READ_SCOPES, WRITE_SCOPES)


TOKEN = g.token_path("gcalendar")


def is_authorized() -> bool: return g.is_authorized(TOKEN, _scopes())
def start_auth_flow() -> dict: return g.start_auth_flow(TOKEN, _scopes(), "gcalendar")
def revoke() -> dict: return g.revoke(TOKEN)


def live_status() -> dict:
    if not g.client_config("gcalendar"):
        return {"status": "unconfigured", "error": "Set up Drive first (Calendar reuses its OAuth client)."}
    if not is_authorized():
        return {"status": "unconfigured", "error": "Click Authorize below."}
    is_write = g.mode("gcalendar") == "readwrite"
    return {"status": "ok", "scopes": _scopes(), "read_only": not is_write,
            "mode": "read+write" if is_write else "read"}


def snapshot() -> dict:
    try:
        svc = g.build_service(TOKEN, _scopes(), "calendar", "v3")
    except Exception as e:
        return {"status": "unconfigured", "error": str(e)}
    try:
        cals = svc.calendarList().list().execute().get("items", [])
        items: list[dict] = []
        now = datetime.now(timezone.utc)
        # window: 90 days back, 180 days forward
        time_min = (now - timedelta(days=90)).isoformat()
        time_max = (now + timedelta(days=180)).isoformat()
        for cal in cals:
            cal_id = cal.get("id")
            cal_summary = cal.get("summary", "")
            page_token = None
            n = 0
            while True:
                r = svc.events().list(
                    calendarId=cal_id, timeMin=time_min, timeMax=time_max,
                    singleEvents=True, orderBy="startTime",
                    maxResults=250, pageToken=page_token,
                ).execute()
                for ev in r.get("items", []):
                    start = ev.get("start", {}) or {}
                    end   = ev.get("end", {}) or {}
                    items.append({
                        "id":       ev.get("id"),
                        "title":    ev.get("summary", "(no title)"),
                        "description": (ev.get("description") or "")[:300],
                        "location": ev.get("location", ""),
                        "start":    start.get("dateTime") or start.get("date"),
                        "end":      end.get("dateTime") or end.get("date"),
                        "calendar": cal_summary,
                        "url":      ev.get("htmlLink"),
                        "status":   ev.get("status"),
                    })
                    n += 1
                page_token = r.get("nextPageToken")
                if not page_token or n >= 500:
                    break
        return {"status": "ok", "synced_at": datetime.now().isoformat(),
                "count": len(items), "items": items,
                "calendars": [{"id": c.get("id"), "summary": c.get("summary"),
                               "primary": c.get("primary", False)} for c in cals]}
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
