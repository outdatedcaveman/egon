"""Routster adapter — reads %APPDATA%/routster/kms_local_data.sqlite.

Read-only & immutable URI to bypass any write-locks Routster may hold.
"""
from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timedelta
from lib.lazy_httpx import httpx  # deferred ~2s import (2026-06-11 perf pass)

DB_PATH = os.path.expandvars(r"%APPDATA%\routster\kms_local_data.sqlite")


def _connect():
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro&immutable=1", uri=True, timeout=2.0)


def _count(c, sql, args=()) -> int:
    try:
        return c.execute(sql, args).fetchone()[0] or 0
    except Exception:
        return 0


def live_status() -> dict:
    """Live Routster state read straight from its SQLite.

    Bruno 2026-05-29: the old version only counted the `links` table, which
    is empty after Routster exports — so the dashboard read "0 links / 0
    queue" even though there's a large backlog. Routster's real state lives
    in `unsorted_archive` (the queue to triage), `action_logs` (everything
    it has routed) and `learned_rules`. We surface all of them. `links`
    stays for completeness but is no longer the headline number.
    """
    if not os.path.exists(DB_PATH):
        return {"status": "error", "error": f"db not found: {DB_PATH}"}
    try:
        now = int(time.time())
        yesterday = now - 86400
        with _connect() as c:
            total_links = _count(c, "SELECT count(*) FROM links")
            unsorted = _count(c, "SELECT count(*) FROM unsorted_archive")
            actions_total = _count(c, "SELECT count(*) FROM action_logs")
            actions_24h = _count(
                c, "SELECT count(*) FROM action_logs WHERE timestamp >= ?",
                (yesterday,))
            learned = _count(c, "SELECT count(*) FROM learned_rules")
            routes_n = _count(c, "SELECT count(*) FROM routes")
            excluded = _count(c, "SELECT count(*) FROM excluded_urls")
            # Most recent activity = newest action log entry.
            last_ts = _count(c, "SELECT max(timestamp) FROM action_logs")

            # 7-day sparkline from routing activity (action_logs).
            spark = []
            for d in range(6, -1, -1):
                lo = now - (d + 1) * 86400
                hi = now - d * 86400
                spark.append(_count(
                    c, "SELECT count(*) FROM action_logs WHERE timestamp BETWEEN ? AND ?",
                    (lo, hi)))

        return {
            "status": "ok",
            # Headline = the backlog you actually need to triage.
            "queue_count": total_links,
            "unsorted_count": unsorted,
            "total_links": total_links,
            "actions_total": actions_total,
            "actions_24h": actions_24h,
            "learned_rules": learned,
            "routes_count": routes_n,
            "excluded_count": excluded,
            "delta_24h": actions_24h,
            "last_activity_iso": datetime.fromtimestamp(last_ts).isoformat() if last_ts else None,
            "spark_7d": spark,
        }
    except sqlite3.DatabaseError as e:
        return {"status": "error", "error": f"sqlite: {e}"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def get_routes() -> list[dict]:
    if not os.path.exists(DB_PATH):
        return []
    try:
        with _connect() as c:
            c.row_factory = sqlite3.Row
            rows = c.execute("SELECT id, category, action_order, connector_id, enabled FROM routes ORDER BY category, action_order").fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []


def get_logs(limit: int = 100) -> list[dict]:
    if not os.path.exists(DB_PATH):
        return []
    try:
        with _connect() as c:
            c.row_factory = sqlite3.Row
            rows = c.execute("SELECT id, entity_title, entity_url, category, connector, message, timestamp FROM action_logs ORDER BY timestamp DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []


def get_unsorted(limit: int = 100) -> list[dict]:
    if not os.path.exists(DB_PATH):
        return []
    try:
        with _connect() as c:
            c.row_factory = sqlite3.Row
            rows = c.execute("SELECT url, title, last_visit, visits, archived_at FROM unsorted_archive ORDER BY archived_at DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []


def trigger_export(item_ids: list[str] = None) -> dict:
    try:
        payload = {}
        if item_ids is not None:
            payload["itemIds"] = item_ids
        r = httpx.post("http://localhost:4000/api/export", json=payload, timeout=10.0)
        return {"status": "ok" if r.status_code in (200, 201, 202) else "error", "code": r.status_code, "text": r.text}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def cancel_export() -> dict:
    try:
        r = httpx.post("http://localhost:4000/api/export/cancel", timeout=5.0)
        return {"status": "ok" if r.status_code in (200, 201, 202) else "error", "code": r.status_code, "text": r.text}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def sync_chrome() -> dict:
    try:
        r = httpx.post("http://localhost:4000/api/sync-chrome", timeout=15.0)
        return r.json() if r.status_code == 200 else {"error": f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as e:
        return {"error": str(e)}


def upload_bookmarks(file_path: str) -> dict:
    try:
        with open(file_path, "rb") as f:
            files = {"file": (os.path.basename(file_path), f, "text/html")}
            r = httpx.post("http://localhost:4000/api/upload-bookmarks", files=files, timeout=30.0)
            return r.json() if r.status_code == 200 else {"error": f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as e:
        return {"error": str(e)}


def add_link(url: str, title: str = "") -> dict:
    try:
        r = httpx.post("http://localhost:4000/api/links", json={"url": url, "title": title}, timeout=10.0)
        return r.json() if r.status_code == 200 else {"error": f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as e:
        return {"error": str(e)}


def get_links() -> list[dict]:
    if not os.path.exists(DB_PATH):
        return []
    try:
        with _connect() as c:
            c.row_factory = sqlite3.Row
            rows = c.execute("SELECT id, category, url, title, description, markdownBody, date_added, source, paperLink, type, filePath, confidence FROM links ORDER BY date_added DESC").fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []


def get_categories() -> list[str]:
    if not os.path.exists(DB_PATH):
        return []
    try:
        import json
        cats = []
        with _connect() as c:
            # 1. read settings categories
            row = c.execute("SELECT value FROM settings WHERE key = 'categories'").fetchone()
            if row:
                try:
                    cats = json.loads(row[0])
                except Exception:
                    pass
            # 2. read from routes
            r_cats = [r[0] for r in c.execute("SELECT DISTINCT category FROM routes WHERE category IS NOT NULL AND category != ''").fetchall()]
            # 3. read from links
            l_cats = [r[0] for r in c.execute("SELECT DISTINCT category FROM links WHERE category IS NOT NULL AND category != '' AND category != 'Uncategorized'").fetchall()]
            
            merged = sorted(list(set(cats + r_cats + l_cats)))
            return merged
    except Exception:
        return []


def update_link(link_id: str, data: dict) -> dict:
    try:
        r = httpx.put(f"http://localhost:4000/api/links/{link_id}", json=data, timeout=5.0)
        return r.json() if r.status_code == 200 else {"error": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"error": str(e)}


def delete_link(link_id: str) -> dict:
    try:
        r = httpx.delete(f"http://localhost:4000/api/links/{link_id}", timeout=5.0)
        return {"status": "ok"} if r.status_code == 200 else {"error": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"error": str(e)}


def mass_delete(item_ids: list[str]) -> dict:
    try:
        r = httpx.post("http://localhost:4000/api/links/mass-delete", json={"itemIds": item_ids}, timeout=10.0)
        return r.json() if r.status_code == 200 else {"error": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"error": str(e)}


def mass_category(item_ids: list[str], category: str) -> dict:
    try:
        r = httpx.post("http://localhost:4000/api/links/mass-category", json={"itemIds": item_ids, "category": category}, timeout=10.0)
        return r.json() if r.status_code == 200 else {"error": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"error": str(e)}


def mass_reclassify(item_ids: list[str]) -> dict:
    try:
        r = httpx.post("http://localhost:4000/api/links/mass-reclassify", json={"itemIds": item_ids}, timeout=15.0)
        return r.json() if r.status_code == 200 else {"error": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"error": str(e)}


def clear_sweep() -> dict:
    try:
        r = httpx.post("http://localhost:4000/api/clear-sweep", timeout=10.0)
        return r.json() if r.status_code == 200 else {"error": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"error": str(e)}


def recover_failed_exports() -> dict:
    try:
        r = httpx.post("http://localhost:4000/api/recover-failed-exports", timeout=30.0)
        return r.json() if r.status_code == 200 else {"error": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"error": str(e)}


def recover_to_zotero() -> dict:
    try:
        r = httpx.post("http://localhost:4000/api/recover-to-zotero", timeout=30.0)
        return r.json() if r.status_code == 200 else {"error": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"error": str(e)}


def recover_to_instapaper() -> dict:
    try:
        r = httpx.post("http://localhost:4000/api/recover-to-instapaper", timeout=30.0)
        return r.json() if r.status_code == 200 else {"error": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"error": str(e)}


def get_all_settings() -> dict:
    try:
        r = httpx.get("http://localhost:4000/api/all-settings", timeout=5.0)
        return r.json() if r.status_code == 200 else {}
    except Exception:
        return {}


def update_setting(section: str, key: str, value) -> dict:
    try:
        r = httpx.patch("http://localhost:4000/api/all-settings", json={"section": section, "key": key, "value": value}, timeout=5.0)
        return r.json() if r.status_code == 200 else {"error": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"error": str(e)}


def ingest_item(type_str: str, value: str, parse_links: bool = False, file_path: str = None) -> dict:
    try:
        if file_path:
            with open(file_path, "rb") as f:
                files = {"file": (os.path.basename(file_path), f)}
                data = {"type": "file", "parseLinks": "true" if parse_links else "false"}
                r = httpx.post("http://localhost:4000/api/ingest", data=data, files=files, timeout=30.0)
        else:
            payload = {
                "type": type_str,
                "parseLinks": parse_links
            }
            if type_str == "url":
                payload["url"] = value
            else:
                payload["textContent"] = value
            r = httpx.post("http://localhost:4000/api/ingest", json=payload, timeout=30.0)
        return r.json() if r.status_code == 200 else {"error": f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as e:
        return {"error": str(e)}


def sweep_history(file_path: str, threshold: int = 8) -> dict:
    try:
        with open(file_path, "rb") as f:
            files = {"file": (os.path.basename(file_path), f)}
            data = {"threshold": str(threshold)}
            r = httpx.post("http://localhost:4000/api/sweep-history", data=data, files=files, timeout=60.0)
            return r.json() if r.status_code == 200 else {"error": f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as e:
        return {"error": str(e)}

