"""Zotero — reads the local SQLite at %APPDATA%/Zotero/Zotero/zotero.sqlite (read-only, immutable URI)."""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path

from lib.snapshot_store import latest_snapshot

META = {
    "id": "zotero",
    "label": "Zotero",
    "icon": "📚",
    "kind": "reference",
    "needs_auth": False,
    "destructive_actions": [],
    "read_only_default": True,
}

CANDIDATES = [
    Path(os.environ.get("APPDATA", "")) / "Zotero" / "Zotero" / "zotero.sqlite",
    Path.home() / "Zotero" / "zotero.sqlite",
]


def _db() -> Path | None:
    for p in CANDIDATES:
        if p.exists():
            return p
    return None


def live_status() -> dict:
    p = _db()
    if not p:
        return {"status": "unconfigured", "error": f"zotero.sqlite not found in {CANDIDATES}"}
    # Report the TRUE reference count (home/dashboard read total_items; without
    # it the source silently looked empty). Fast COUNT(*), not a full fetch.
    # Bruno 2026-06-12: was undercounting at the old LIMIT 5000.
    total = None
    try:
        c = sqlite3.connect(f"file:{p}?mode=ro&immutable=1", uri=True, timeout=10.0)
        total = c.execute("""
            SELECT COUNT(*) FROM items
            WHERE itemTypeID NOT IN (
                SELECT itemTypeID FROM itemTypes
                WHERE typeName IN ('attachment','note','annotation'))""").fetchone()[0]
        c.close()
    except Exception:
        pass
    out = {"status": "ok", "path": str(p),
           "size_mb": round(p.stat().st_size / 1_000_000, 1)}
    if total is not None:
        out["total_items"] = total
    return out


def snapshot() -> dict:
    p = _db()
    if not p:
        return {"status": "unconfigured", "error": "zotero.sqlite not found"}
    try:
        c = sqlite3.connect(f"file:{p}?mode=ro&immutable=1", uri=True, timeout=10.0)
        cur = c.cursor()
        # No artificial cap: Bruno's library is ~252k real items and the old
        # LIMIT 5000 was the entire "Zotero has 5k" undercount (2026-06-12).
        # Also exclude notes/annotations, not just attachments, so the count
        # is true reference items.
        cur.execute("""
            SELECT it.itemID, it.dateAdded, idv_t.value AS title, idv_d.value AS doi,
                   idv_a.value AS abstract, idv_u.value AS url
            FROM items it
            LEFT JOIN itemData id_t ON id_t.itemID = it.itemID AND id_t.fieldID = (SELECT fieldID FROM fields WHERE fieldName='title')
            LEFT JOIN itemDataValues idv_t ON idv_t.valueID = id_t.valueID
            LEFT JOIN itemData id_d ON id_d.itemID = it.itemID AND id_d.fieldID = (SELECT fieldID FROM fields WHERE fieldName='DOI')
            LEFT JOIN itemDataValues idv_d ON idv_d.valueID = id_d.valueID
            LEFT JOIN itemData id_a ON id_a.itemID = it.itemID AND id_a.fieldID = (SELECT fieldID FROM fields WHERE fieldName='abstractNote')
            LEFT JOIN itemDataValues idv_a ON idv_a.valueID = id_a.valueID
            LEFT JOIN itemData id_u ON id_u.itemID = it.itemID AND id_u.fieldID = (SELECT fieldID FROM fields WHERE fieldName='url')
            LEFT JOIN itemDataValues idv_u ON idv_u.valueID = id_u.valueID
            WHERE it.itemTypeID NOT IN (
                SELECT itemTypeID FROM itemTypes
                WHERE typeName IN ('attachment','note','annotation'))
            ORDER BY it.dateAdded DESC
        """)
        rows = []
        for itemID, dateAdded, title, doi, abstract, url in cur.fetchall():
            rows.append({"id": itemID, "added": dateAdded,
                         "title": title or "(untitled)", "doi": doi or "",
                         "url": url or "",
                         "abstract": (abstract or "")[:2000]})
        c.close()
    except sqlite3.DatabaseError as e:
        return {"status": "error", "error": f"sqlite: {e}"}

    return {
        "status": "ok",
        "synced_at": datetime.now().isoformat(),
        "count": len(rows),
        "items": rows,
    }


def items(limit: int = 300000) -> list[dict]:
    """Direct read from the Zotero SQLite — full set of fields.

    Bypasses snapshot_store so the UI always sees the live DB, with every
    column the data-browser needs: title · creators · year · DOI · URL ·
    publication · tags · dateAdded. SQLite reads on a 1.5 GB Zotero DB take
    ~50 ms — no need to cache via snapshot.
    """
    p = _db()
    if not p:
        return []
    try:
        c = sqlite3.connect(f"file:{p}?mode=ro&immutable=1", uri=True, timeout=3.0)
        cur = c.cursor()
        # One query that joins every field we want. We pull the fieldID for
        # each field up front, then LEFT JOIN itemData for each. Tags +
        # creators come from sub-queries that we group_concat.
        cur.execute("SELECT fieldID, fieldName FROM fields")
        fields = {n: i for (i, n) in cur.fetchall()}
        def fid(name: str) -> int:
            return fields.get(name, -1)
        sql = f"""
            SELECT
              it.itemID,
              it.dateAdded,
              v_title.value          AS title,
              v_doi.value            AS doi,
              v_url.value            AS url,
              v_date.value           AS pub_date,
              v_publication.value    AS publication,
              v_book_title.value     AS book_title,
              v_abstract.value       AS abstract,
              (SELECT GROUP_CONCAT(c.lastName, ', ')
                 FROM itemCreators ic
                 JOIN creators c ON c.creatorID = ic.creatorID
                WHERE ic.itemID = it.itemID
                ORDER BY ic.orderIndex)  AS authors,
              (SELECT GROUP_CONCAT(t.name, '; ')
                 FROM itemTags itg JOIN tags t ON t.tagID = itg.tagID
                WHERE itg.itemID = it.itemID)        AS tags
            FROM items it
            LEFT JOIN itemData id_title ON id_title.itemID = it.itemID AND id_title.fieldID = {fid('title')}
            LEFT JOIN itemDataValues v_title ON v_title.valueID = id_title.valueID
            LEFT JOIN itemData id_doi   ON id_doi.itemID   = it.itemID AND id_doi.fieldID   = {fid('DOI')}
            LEFT JOIN itemDataValues v_doi   ON v_doi.valueID   = id_doi.valueID
            LEFT JOIN itemData id_url   ON id_url.itemID   = it.itemID AND id_url.fieldID   = {fid('url')}
            LEFT JOIN itemDataValues v_url   ON v_url.valueID   = id_url.valueID
            LEFT JOIN itemData id_date  ON id_date.itemID  = it.itemID AND id_date.fieldID  = {fid('date')}
            LEFT JOIN itemDataValues v_date  ON v_date.valueID  = id_date.valueID
            LEFT JOIN itemData id_pub   ON id_pub.itemID   = it.itemID AND id_pub.fieldID   = {fid('publicationTitle')}
            LEFT JOIN itemDataValues v_publication ON v_publication.valueID = id_pub.valueID
            LEFT JOIN itemData id_book  ON id_book.itemID  = it.itemID AND id_book.fieldID  = {fid('bookTitle')}
            LEFT JOIN itemDataValues v_book_title ON v_book_title.valueID = id_book.valueID
            LEFT JOIN itemData id_abs   ON id_abs.itemID   = it.itemID AND id_abs.fieldID   = {fid('abstractNote')}
            LEFT JOIN itemDataValues v_abstract ON v_abstract.valueID = id_abs.valueID
            WHERE it.itemTypeID NOT IN (
                SELECT itemTypeID FROM itemTypes
                WHERE typeName IN ('attachment','note','annotation'))
            ORDER BY it.dateAdded DESC
            LIMIT ?
        """
        cur.execute(sql, (limit,))
        rows = []
        for r in cur.fetchall():
            (itemID, dateAdded, title, doi, url, pub_date, publication,
             book_title, abstract, authors, tags) = r
            # Extract a 4-digit year from pub_date if present
            year = ""
            if pub_date:
                import re
                m = re.search(r"(19|20)\d{2}", str(pub_date))
                if m:
                    year = m.group(0)
            rows.append({
                "id":          itemID,
                "title":       title or "(untitled)",
                "authors":     authors or "",
                "year":        year,
                "doi":         doi or "",
                "url":         url or "",
                "publication": publication or book_title or "",
                "date":        pub_date or "",
                "added":       (dateAdded or "")[:10],
                "tags":        tags or "",
                "abstract":    (abstract or "")[:300],
            })
        c.close()
        return rows
    except sqlite3.DatabaseError:
        return []


def library_stats() -> dict:
    """Full-library aggregates via fast SQL (COUNT + GROUP BY) — accurate
    even at 250k items, without loading them. Bruno 2026-05-22: the stats
    bar must reflect the WHOLE database, not the rendered window."""
    p = _db()
    if not p:
        return {}
    try:
        c = sqlite3.connect(f"file:{p}?mode=ro&immutable=1", uri=True, timeout=4.0)
        non_item = "(SELECT itemTypeID FROM itemTypes WHERE typeName IN ('attachment','note'))"
        total = c.execute(
            f"SELECT COUNT(*) FROM items WHERE itemTypeID NOT IN {non_item}").fetchone()[0]
        by_type = {}
        for name, n in c.execute(f"""
            SELECT t.typeName, COUNT(*) FROM items it
            JOIN itemTypes t ON t.itemTypeID = it.itemTypeID
            WHERE it.itemTypeID NOT IN {non_item}
            GROUP BY t.typeName ORDER BY COUNT(*) DESC LIMIT 8"""):
            by_type[name] = n
        last = c.execute(
            f"SELECT MAX(dateAdded) FROM items WHERE itemTypeID NOT IN {non_item}").fetchone()[0]
        c.close()
        return {"total": total, "by_type": by_type, "last_updated": (last or "")[:10]}
    except Exception:
        return {}


def stats() -> dict:
    snap = latest_snapshot(META["id"])
    if not snap:
        return {"status": "no-snapshot", "count": 0, "last_synced": None,
                "error": "click Sync now to pull first snapshot"}
    return {
        "status": snap.get("status", "ok"),
        "count": snap.get("count", 0),
        "last_synced": (snap.get("synced_at") or "")[:16],
    }
