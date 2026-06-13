"""YouTube Takeout importer — the COMPLETE watch history.

Why this exists (Bruno 2026-06-12: "I want all my data"): Google removed
watch history from the YouTube Data API in September 2016 — no app, key or
scope can fetch it. The extension scrapes youtube.com/feed/history, but that
only yields what the page shows per visit (~200 recent items, accumulating
slowly now that harvests merge). The ONLY complete source is Google Takeout:

    1. takeout.google.com → Deselect all → check "YouTube and YouTube Music"
    2. "All YouTube data included" → keep only "history"
    3. Export → download the zip
    4. Drop the zip (or the extracted watch-history.json/html) into
       state/inbox/takeout/   — this importer does the rest.

import_takeout() finds the newest watch-history file there (json preferred,
the html fallback is parsed too), normalizes every entry and MERGES it into
the same harvest state the extension feeds (state/panop/
youtube_history_state.json), so the snapshot/mirror/index pipeline picks the
full history up automatically. Runs with the daily snapshots unit — dropping
a new Takeout in is all Bruno ever has to do.
"""
from __future__ import annotations

import json
import re
import time
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INBOX = ROOT / "state" / "inbox" / "takeout"
STATE = ROOT / "state" / "panop" / "youtube_history_state.json"
MARK = INBOX / "_imported.json"


def _candidates() -> list[Path]:
    if not INBOX.is_dir():
        return []
    out = []
    for p in INBOX.rglob("*"):
        n = p.name.lower()
        if n.endswith(".zip") or "watch-history" in n or \
                ("histórico" in n and n.endswith((".json", ".html"))):
            out.append(p)
    return sorted(out, key=lambda p: p.stat().st_mtime, reverse=True)


def _entries_from_json(text: str) -> list[dict]:
    try:
        data = json.loads(text)
    except Exception:
        return []
    out = []
    for e in data if isinstance(data, list) else []:
        title = (e.get("title") or "").removeprefix("Watched ").strip()
        url = (e.get("titleUrl") or "").strip()
        if not title or "youtube" not in (e.get("header") or "YouTube").lower():
            continue
        sub = (e.get("subtitles") or [{}])[0]
        out.append({"title": title[:300], "url": url,
                    "channel": (sub.get("name") or "")[:120],
                    "when": (e.get("time") or "")[:19]})
    return out


def _entries_from_html(text: str) -> list[dict]:
    # Takeout html: <a href="watch?v=...">Title</a> ... <a>Channel</a> ... date
    out = []
    for m in re.finditer(
            r'href="(https://www\.youtube\.com/watch[^"]+)"[^>]*>([^<]+)</a>',
            text):
        out.append({"title": m.group(2)[:300], "url": m.group(1),
                    "channel": "", "when": ""})
    return out


def import_takeout() -> dict:
    """Find + import the newest Takeout drop; merge into the harvest state.
    Idempotent: a file already in _imported.json (by name+mtime) is skipped."""
    cands = _candidates()
    if not cands:
        return {"status": "nothing_to_import",
                "hint": f"drop a Takeout zip or watch-history.json into {INBOX}"}
    try:
        seen = json.loads(MARK.read_text(encoding="utf-8"))
    except Exception:
        seen = {}

    src = cands[0]
    sig = f"{src.name}:{int(src.stat().st_mtime)}"
    if seen.get(sig):
        return {"status": "already_imported", "file": src.name}

    entries: list[dict] = []
    if src.suffix.lower() == ".zip":
        with zipfile.ZipFile(src) as z:
            for n in z.namelist():
                low = n.lower()
                if "watch-history" in low or "histórico de visualização" in low:
                    text = z.read(n).decode("utf-8", "replace")
                    entries = (_entries_from_json(text) if low.endswith(".json")
                               else _entries_from_html(text))
                    if entries:
                        break
    else:
        text = src.read_text(encoding="utf-8", errors="replace")
        entries = (_entries_from_json(text) if src.suffix.lower() == ".json"
                   else _entries_from_html(text))

    if not entries:
        return {"status": "no_entries", "file": src.name}

    # merge into the harvest state (same key logic as the server store)
    try:
        cur = json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        cur = {}
    merged = {}
    for it in (cur.get("items") or []):
        k = it.get("url") or it.get("title")
        if k:
            merged[str(k)] = it
    new_n = 0
    for it in entries:
        k = str(it.get("url") or it.get("title"))
        if k not in merged:
            new_n += 1
        merged[k] = {**merged.get(k, {}), **it}
    cur["items"] = list(merged.values())
    cur["count"] = len(cur["items"])
    cur["takeout_imported_at"] = datetime.now().isoformat(timespec="seconds")
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(cur, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    seen[sig] = {"at": time.time(), "entries": len(entries), "new": new_n}
    MARK.parent.mkdir(parents=True, exist_ok=True)
    MARK.write_text(json.dumps(seen, indent=1), encoding="utf-8")
    return {"status": "ok", "file": src.name, "entries": len(entries),
            "new": new_n, "total_now": cur["count"]}
