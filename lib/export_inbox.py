"""Universal data-export inbox — drop any vendor's export zip, Egon eats it.

Bruno 2026-06-12: Takeout is coming with YouTube + Fit + Health + Gemini
etc., and the 'stubborn' sources (Kindle, TV Time) are exactly the ones
whose GDPR/CCPA exports are the only complete data source. One inbox, one
pattern:

    state/inbox/   ← drop ANY export zip here (Takeout, TV Time, Amazon…)

Each scan (rides the daily snapshots unit):
  1. detect the vendor from the zip's contents,
  2. extract to state/exports/<vendor>/<zipname>/ (zip itself never deleted),
  3. run every structured parser that recognizes files inside:
       google: YouTube watch-history → youtube_history harvest state
               My Activity (incl. Gemini/Bard) → google_activity snapshot
               Fit daily metrics → google_fit snapshot
       tvtime: tracking JSONs → tvtime harvest state (merge)
       amazon: Kindle library CSV/JSON → kindle harvest state (merge)
  4. whatever no parser recognizes is still INDEXED: the extraction dir is a
     file_indexer root, so every extracted file surfaces in Artifacts and
     the Connect index regardless.

Idempotent by zip name+mtime (state/inbox/_imported.json).
"""
from __future__ import annotations

import csv
import io
import json
import re
import time
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INBOX = ROOT / "state" / "inbox"
EXPORTS = ROOT / "state" / "exports"
MARK = INBOX / "_imported.json"
PANOP = ROOT / "state" / "panop"


# ── vendor detection ─────────────────────────────────────────────────────────
def _detect_vendor(names: list[str]) -> str:
    joined = "\n".join(names[:400]).lower()
    if "takeout/" in joined or "watch-history" in joined or "fit/" in joined:
        return "google"
    if "tvtime" in joined or "seen_episode" in joined or "tracking-prod" in joined:
        return "tvtime"
    if "digital.content" in joined or "kindle" in joined or "amazon" in joined:
        return "amazon"
    return "unknown"


# ── harvest-state merge (same semantics as the panop server store) ──────────
def _merge_state(path: Path, items: list[dict]) -> int:
    try:
        cur = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        cur = {}
    def key(it):
        return str(it.get("url") or it.get("id") or it.get("asin")
                   or it.get("title") or "")
    merged = {key(it): it for it in (cur.get("items") or []) if key(it)}
    new = 0
    for it in items:
        k = key(it)
        if not k:
            continue
        if k not in merged:
            new += 1
        merged[k] = {**merged.get(k, {}), **it}
    cur["items"] = list(merged.values())
    cur["count"] = len(cur["items"])
    cur["export_imported_at"] = datetime.now().isoformat(timespec="seconds")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cur, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return new


def _write_snapshot(source: str, items: list[dict]) -> None:
    if not items:
        return
    from lib.snapshot_store import write_snapshot
    write_snapshot(source, {"status": "ok", "count": len(items),
                            "synced_at": datetime.now().isoformat(),
                            "items": items})


# ── google parsers ───────────────────────────────────────────────────────────
def _google_parsers(ex_dir: Path, report: dict) -> None:
    # YouTube watch history → reuse the dedicated importer's parsing by
    # merging into the same harvest state.
    from lib.youtube_takeout import _entries_from_json, _entries_from_html
    for p in ex_dir.rglob("*"):
        n = p.name.lower()
        if "watch-history" in n and p.suffix.lower() in (".json", ".html"):
            text = p.read_text(encoding="utf-8", errors="replace")
            entries = (_entries_from_json(text) if p.suffix.lower() == ".json"
                       else _entries_from_html(text))
            items = [{"id": e.get("url") or e.get("title"), **e,
                      "kind": "watched_video"} for e in entries]
            report["youtube_history"] = _merge_state(
                PANOP / "youtube_history_state.json", items)

    # My Activity (per product: Gemini/"Bard", Search, Maps, …) — JSON files
    act_items: list[dict] = []
    for p in ex_dir.rglob("*.json"):
        low = str(p).lower()
        if "my activity" not in low and "myactivity" not in low:
            continue
        product = p.parent.name[:40]
        try:
            data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        for e in data if isinstance(data, list) else []:
            title = (e.get("title") or "").strip()
            if not title:
                continue
            act_items.append({
                "id": f"gact:{product}:{e.get('time','')}:{title[:60]}",
                "title": title[:300],
                "url": e.get("titleUrl") or "",
                "subtitle": " · ".join(x for x in (
                    product, (e.get("time") or "")[:19]) if x)[:200],
                "kind": f"activity:{product.lower().replace(' ', '_')}",
                "when": (e.get("time") or "")[:19],
                "content": " ".join(e.get("subtitles", [{}])[0].get("name", "")
                                    for _ in [0])[:300],
            })
    if act_items:
        _write_snapshot("google_activity", act_items)
        report["google_activity"] = len(act_items)

    # Fit — daily activity metrics CSV (one row per day)
    fit_items: list[dict] = []
    for p in ex_dir.rglob("Daily activity metrics*.csv"):
        try:
            rows = list(csv.DictReader(io.StringIO(
                p.read_text(encoding="utf-8", errors="replace"))))
        except Exception:
            continue
        for r in rows:
            day = r.get("Date") or ""
            if not day:
                continue
            stats = {k: v for k, v in r.items()
                     if v and k != "Date"}
            fit_items.append({
                "id": f"fit:{day}",
                "title": f"Fit — {day}",
                "subtitle": " · ".join(f"{k}: {v}" for k, v in
                                       list(stats.items())[:5])[:200],
                "kind": "fit_daily",
                "when": day,
                "content": json.dumps(stats)[:1500],
            })
    if fit_items:
        _write_snapshot("google_fit", fit_items)
        report["google_fit"] = len(fit_items)


# ── tvtime parser ────────────────────────────────────────────────────────────
def _tvtime_parser(ex_dir: Path, report: dict) -> None:
    items: list[dict] = []
    for p in ex_dir.rglob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        rows = data if isinstance(data, list) else \
            data.get("data") or data.get("episodes") or []
        for e in rows if isinstance(rows, list) else []:
            if not isinstance(e, dict):
                continue
            show = (e.get("show_name") or e.get("series_name")
                    or (e.get("show") or {}).get("name") if
                    isinstance(e.get("show"), dict) else e.get("show")) or ""
            ep = e.get("episode_name") or e.get("name") or ""
            num = e.get("episode_number") or e.get("number") or ""
            season = e.get("season_number") or e.get("season") or ""
            when = (e.get("watched_at") or e.get("created_at") or "")[:19]
            title = " — ".join(x for x in (str(show), f"S{season}E{num}"
                               if season or num else "", str(ep)) if x)
            if not title.strip(" —"):
                continue
            items.append({"id": f"tvt:{show}:{season}:{num}",
                          "title": title[:300], "kind": "watched_episode",
                          "subtitle": when, "when": when})
    if items:
        report["tvtime"] = _merge_state(PANOP / "tvtime_library_state.json",
                                        items)


# ── amazon parser ────────────────────────────────────────────────────────────
def _amazon_parser(ex_dir: Path, report: dict) -> None:
    items: list[dict] = []
    for p in ex_dir.rglob("*.csv"):
        low = p.name.lower()
        if "kindle" not in low and "digital" not in low and "content" not in low:
            continue
        try:
            rows = list(csv.DictReader(io.StringIO(
                p.read_text(encoding="utf-8", errors="replace"))))
        except Exception:
            continue
        for r in rows:
            title = (r.get("Title") or r.get("Product Name")
                     or r.get("title") or "")
            if not title:
                continue
            asin = r.get("ASIN") or r.get("asin") or ""
            items.append({
                "id": asin or title, "asin": asin, "title": title[:300],
                "kind": (r.get("Content Type") or "kindle_item")[:40],
                "subtitle": " · ".join(x for x in (
                    r.get("Author"), (r.get("Acquisition Date")
                                      or r.get("Date") or "")[:10]) if x)[:200],
            })
    if items:
        report["kindle"] = _merge_state(PANOP / "kindle_library_state.json",
                                        items)


_PARSERS = {"google": _google_parsers, "tvtime": _tvtime_parser,
            "amazon": _amazon_parser}


# ── main entry ───────────────────────────────────────────────────────────────
def process() -> dict:
    """Scan the inbox, import anything new. Returns a per-zip report."""
    if not INBOX.is_dir():
        INBOX.mkdir(parents=True, exist_ok=True)
        return {"status": "empty"}
    try:
        seen = json.loads(MARK.read_text(encoding="utf-8"))
    except Exception:
        seen = {}

    out: dict = {}
    for z in sorted(INBOX.rglob("*.zip")):
        sig = f"{z.name}:{int(z.stat().st_mtime)}"
        if seen.get(sig):
            continue
        report: dict = {}
        try:
            with zipfile.ZipFile(z) as zf:
                vendor = _detect_vendor(zf.namelist())
                ex_dir = EXPORTS / vendor / re.sub(r"\W+", "_", z.stem)[:60]
                ex_dir.mkdir(parents=True, exist_ok=True)
                zf.extractall(ex_dir)
            report["vendor"] = vendor
            report["extracted_to"] = str(ex_dir)
            parser = _PARSERS.get(vendor)
            if parser:
                parser(ex_dir, report)
        except Exception as e:
            report["error"] = str(e)[:160]
        seen[sig] = {"at": time.time(), **{k: v for k, v in report.items()
                                           if k != "extracted_to"}}
        out[z.name] = report
    MARK.parent.mkdir(parents=True, exist_ok=True)
    MARK.write_text(json.dumps(seen, indent=1), encoding="utf-8")
    return {"status": "ok", "imported": out} if out else {"status": "nothing_new"}
