"""Google Fit — steps + active minutes + heart rate (last 30 days)."""
from __future__ import annotations

import time
from datetime import datetime, timedelta

from lib import google_oauth as g
from lib.snapshot_store import latest_snapshot

META = {
    "id": "gfit",
    "label": "Google Fit",
    "icon": "💪",
    "kind": "database",
    "needs_auth": True,
    "destructive_actions": [],
    "read_only_default": True,
}

SCOPES = [
    "https://www.googleapis.com/auth/fitness.activity.read",
    "https://www.googleapis.com/auth/fitness.body.read",
    "https://www.googleapis.com/auth/fitness.heart_rate.read",
]
TOKEN = g.token_path("gfit")


def is_authorized() -> bool: return g.is_authorized(TOKEN, SCOPES)
def start_auth_flow() -> dict: return g.start_auth_flow(TOKEN, SCOPES, "gfit")
def revoke() -> dict: return g.revoke(TOKEN)


def live_status() -> dict:
    if not g.client_config("gfit"):
        return {"status": "unconfigured", "error": "Set up Drive first."}
    if not is_authorized():
        return {"status": "unconfigured", "error": "Click Authorize below."}
    return {"status": "ok", "scopes": SCOPES, "read_only": True}


def _ns(dt: datetime) -> int:
    return int(dt.timestamp() * 1e9)


def snapshot() -> dict:
    try:
        svc = g.build_service(TOKEN, SCOPES, "fitness", "v1")
    except Exception as e:
        return {"status": "unconfigured", "error": str(e)}
    try:
        end = datetime.now()
        start = end - timedelta(days=30)
        # Aggregate steps by day
        body = {
            "aggregateBy": [{"dataTypeName": "com.google.step_count.delta"}],
            "bucketByTime": {"durationMillis": 86_400_000},
            "startTimeMillis": int(start.timestamp() * 1000),
            "endTimeMillis":   int(end.timestamp() * 1000),
        }
        agg = svc.users().dataset().aggregate(userId="me", body=body).execute()
        items: list[dict] = []
        for bucket in agg.get("bucket", []):
            day_start = int(bucket.get("startTimeMillis", 0)) // 1000
            steps = 0
            for ds in bucket.get("dataset", []):
                for pt in ds.get("point", []):
                    for v in pt.get("value", []):
                        if "intVal" in v:
                            steps += v["intVal"]
            items.append({
                "type":  "steps",
                "title": f"Steps · {datetime.fromtimestamp(day_start):%Y-%m-%d}",
                "date":  datetime.fromtimestamp(day_start).isoformat(),
                "steps": steps,
            })
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
