"""Mouseion adapter — talks to its local Flask service OR reads its SQLite directly.

Path priority:
1. Flask running on http://127.0.0.1:7274 → call its REST API
2. Local SQLite at user-configured path → read directly (read-only/immutable URI)
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path

from lib.lazy_httpx import httpx  # deferred ~2s import (2026-06-11 perf pass)

from lib import secrets
from lib.snapshot_store import latest_snapshot
from lib.egon_paths import MOUSEION_DB

META = {
    "id": "mouseion",
    "label": "Mouseion",
    "icon": "🐭",
    "kind": "reference",
    "needs_auth": False,
    "destructive_actions": [],
    "read_only_default": True,
}

FLASK_URL = "http://127.0.0.1:7274"

# Candidate locations probed in order. The first one that exists AND has data wins.
# Mouseion's real DB lives in the platform appdata dir, NOT alongside the .exe —
# this caused the "empty SQLite" false alarm.
_DB_CANDIDATES = [
    Path.home() / ".local" / "share" / "mouseion" / "refs.db",
    Path.home() / "AppData" / "Local" / "mouseion" / "refs.db",
    Path.home() / "AppData" / "Roaming" / "mouseion" / "refs.db",
    Path.home() / ".local" / "share" / "zoterpile" / "refs.db",
    MOUSEION_DB,
]


def _db_path() -> Path:
    """Resolve refs.db: explicit config > first non-empty candidate > first existing > first candidate."""
    cfg = secrets.get("mouseion.path")
    if cfg and Path(cfg).exists() and Path(cfg).stat().st_size > 0:
        return Path(cfg)
    # Prefer non-empty existing
    non_empty = [p for p in _DB_CANDIDATES if p.exists() and p.stat().st_size > 1024]
    if non_empty:
        return max(non_empty, key=lambda p: p.stat().st_size)
    # Any existing
    existing = [p for p in _DB_CANDIDATES if p.exists()]
    if existing:
        return existing[0]
    return Path(cfg) if cfg else _DB_CANDIDATES[0]


def detected_db_path() -> str:
    """Public helper for Settings UI — what we'd use right now."""
    return str(_db_path())


def _flask_up() -> bool:
    try:
        r = httpx.get(f"{FLASK_URL}/", timeout=1.5)
        return r.status_code < 500
    except Exception:
        return False


def live_status() -> dict:
    if _flask_up():
        return {"status": "ok", "source": "flask", "url": FLASK_URL}
    p = _db_path()
    if not p.exists():
        return {"status": "unconfigured",
                "error": f"refs.db not found at {p}. Set mouseion.path in Settings."}
    try:
        c = sqlite3.connect(f"file:{p}?mode=ro&immutable=1", uri=True, timeout=2)
        tables = [r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        c.close()
        if not tables:
            return {"status": "warn",
                    "error": "DB found but empty (no tables). Run Mouseion at least once to initialize it."}
        return {"status": "ok", "source": "sqlite", "tables": tables}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def snapshot() -> dict:
    # Try Flask first
    if _flask_up():
        try:
            r = httpx.get(f"{FLASK_URL}/api/refs", timeout=20)
            if r.status_code == 200:
                data = r.json()
                items = data if isinstance(data, list) else data.get("items", [])
                return {"status": "ok", "source": "flask",
                        "synced_at": datetime.now().isoformat(),
                        "count": len(items), "items": items}
        except Exception:
            pass

    # Fall back to SQLite
    p = _db_path()
    if not p.exists():
        return {"status": "unconfigured", "error": f"not found: {p}"}
    try:
        c = sqlite3.connect(f"file:{p}?mode=ro&immutable=1", uri=True, timeout=3)
        tables = [r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        if not tables:
            return {"status": "warn",
                    "error": "Mouseion SQLite is empty. Start Mouseion + import refs first.",
                    "items": [], "count": 0,
                    "synced_at": datetime.now().isoformat()}
        # Try common table names — refs, references, items
        items: list[dict] = []
        total = 0
        for t in ("refs", "references", "items", "papers"):
            if t in tables:
                try:
                    total = c.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                    cur = c.execute(f'SELECT * FROM "{t}" ORDER BY created_at DESC LIMIT 5000')
                    cols = [d[0] for d in cur.description]
                    for row in cur.fetchall():
                        raw = dict(zip(cols, row))
                        # Normalize to the same shape the References view expects.
                        items.append({
                            "id":       raw.get("id"),
                            "title":    raw.get("title", "") or "",
                            "creators": raw.get("authors", "") or "",
                            "year":     str(raw.get("year") or "")[:4],
                            "type":     raw.get("ref_type", "") or "",
                            "doi":      raw.get("doi", "") or "",
                            "url":      raw.get("url", "") or raw.get("oa_url", "") or "",
                            "journal":  raw.get("journal", "") or raw.get("container_title", "") or "",
                            "added":    str(raw.get("created_at") or "")[:10],
                            "abstract": (raw.get("abstract") or "")[:400],
                        })
                    break
                except Exception:
                    continue
        c.close()
        return {"status": "ok", "source": "sqlite",
                "synced_at": datetime.now().isoformat(),
                "count": len(items), "total_in_library": total,
                "items": items, "tables": tables}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def items(limit: int = 5000) -> list[dict]:
    """Direct read from refs.db. Bypasses snapshot_store so the UI always
    sees the live DB. Normalises field names to the References-page schema
    (title/authors/year/publication/doi/url/added/tags). Bruno 2026-05-20:
    items() used to require a snapshot() pre-run; now it just reads."""
    # Try Flask first — gives the freshest data
    if _flask_up():
        try:
            r = httpx.get(f"{FLASK_URL}/api/refs?limit={limit}", timeout=10)
            if r.status_code == 200:
                data = r.json()
                raws = data if isinstance(data, list) else data.get("items", [])
                return [_normalize(raw) for raw in raws]
        except Exception:
            pass
    # Direct SQLite read
    p = _db_path()
    if not p.exists():
        return []
    try:
        c = sqlite3.connect(f"file:{p}?mode=ro&immutable=1", uri=True, timeout=3)
        tables = [r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        # Try common table names — first one that has rows wins
        for t in ("refs", "references", "items", "papers"):
            if t not in tables:
                continue
            try:
                cur = c.execute(f'SELECT * FROM "{t}" '
                                f'ORDER BY COALESCE(created_at, "") DESC LIMIT ?',
                                (limit,))
            except sqlite3.OperationalError:
                cur = c.execute(f'SELECT * FROM "{t}" LIMIT ?', (limit,))
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
            c.close()
            return [_normalize(r) for r in rows]
        c.close()
    except sqlite3.DatabaseError:
        pass
    return []


def _clean_authors(val) -> str:
    """Mouseion stores authors as a JSON array of {family, given} objects
    (often as a TEXT column). Render it as 'Family G., Family2 G2.'.
    Falls back to the raw string if it isn't parseable. Bruno 2026-05-22:
    the table was showing raw JSON instead of names."""
    if not val:
        return ""
    data = val
    if isinstance(val, str):
        s = val.strip()
        if s.startswith("["):
            try:
                import json as _j
                data = _j.loads(s)
            except Exception:
                return s[:200]
        else:
            return s[:200]
    if isinstance(data, list):
        names = []
        for a in data:
            if isinstance(a, dict):
                fam = a.get("family") or a.get("last") or a.get("name") or ""
                giv = a.get("given") or a.get("first") or ""
                initials = "".join(p[0] + "." for p in giv.split() if p) if giv else ""
                nm = (fam + (" " + initials if initials else "")).strip()
                if nm:
                    names.append(nm)
            elif isinstance(a, str):
                names.append(a)
        return ", ".join(names)[:300]
    return str(data)[:200]


def _titlecase_if_lower(t: str) -> str:
    """Mouseion sometimes lowercases titles. Restore title case only when the
    whole thing is lowercase (don't disturb properly-cased titles)."""
    if t and t == t.lower() and any(c.isalpha() for c in t):
        # capitalise first letter of each significant word
        small = {"a","an","the","of","and","or","to","in","on","for","with","is","via"}
        words = t.split()
        out = []
        for i, w in enumerate(words):
            out.append(w if (i and w in small) else (w[:1].upper() + w[1:]))
        return " ".join(out)
    return t


def _normalize(raw: dict) -> dict:
    """Map a raw Mouseion row to the References-page schema."""
    return {
        "id":          raw.get("id"),
        "title":       _titlecase_if_lower((raw.get("title") or raw.get("Title") or "").strip()),
        "authors":     _clean_authors(raw.get("authors") or raw.get("author") or raw.get("creators")),
        "year":        str(raw.get("year") or raw.get("published_year") or "")[:4],
        "publication": raw.get("journal") or raw.get("container_title")
                        or raw.get("publication") or "",
        "doi":         raw.get("doi") or "",
        "url":         raw.get("url") or raw.get("oa_url") or "",
        "added":       str(raw.get("created_at") or raw.get("date_added") or "")[:10],
        "tags":        raw.get("tags") or "",
        "abstract":    (raw.get("abstract") or "")[:300],
    }


def library_stats() -> dict:
    """Full-library aggregates via fast SQL — accurate at 250k refs without
    loading them all. Bruno 2026-05-22."""
    p = _db_path()
    if not p.exists():
        return {}
    try:
        c = sqlite3.connect(f"file:{p}?mode=ro&immutable=1", uri=True, timeout=4.0)
        tables = [r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        tbl = next((t for t in ("refs", "references", "items", "papers") if t in tables), None)
        if not tbl:
            c.close(); return {}
        total = c.execute(f'SELECT COUNT(*) FROM "{tbl}"').fetchone()[0]
        cols = [r[1] for r in c.execute(f'PRAGMA table_info("{tbl}")')]
        by_type = {}
        type_col = next((cc for cc in ("ref_type", "type", "itemType") if cc in cols), None)
        if type_col:
            for name, n in c.execute(
                    f'SELECT COALESCE("{type_col}", "—"), COUNT(*) FROM "{tbl}" '
                    f'GROUP BY "{type_col}" ORDER BY COUNT(*) DESC LIMIT 8'):
                by_type[str(name)] = n
        last = ""
        date_col = next((cc for cc in ("created_at", "date_added", "added") if cc in cols), None)
        if date_col:
            last = (c.execute(f'SELECT MAX("{date_col}") FROM "{tbl}"').fetchone()[0] or "")[:10]
        c.close()
        return {"total": total, "by_type": by_type, "last_updated": last}
    except Exception:
        return {}


def stats() -> dict:
    s = latest_snapshot(META["id"])
    if not s: return {"status": "no-snapshot", "count": 0, "last_synced": None}
    return {"status": s.get("status", "ok"), "count": s.get("count", 0),
            "last_synced": (s.get("synced_at") or "")[:16]}
