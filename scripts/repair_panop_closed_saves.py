"""Repair Panop close-era saves.

Old Panop drain logs recorded aggregate close counts but not per-tab URLs.
Therefore the repair scope is every categorized Panop history row: if a row
could have been closed by Egon, verify it exists in both Chrome bookmarks and
Zotero; if not, save it.

Default is dry run. Use --commit to write.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "external" / "panop_server"))

import main as panop_main  # type: ignore  # noqa: E402
from lib import secrets  # noqa: E402

panop_main.ENV_FILE = str(ROOT / "external" / "panop_server" / "panop_env.json")

LOG_FILE = ROOT / "state" / "panop" / "panop_repair_closed_saves.jsonl"

_real_get_env = panop_main.get_env


def _get_env_with_secrets():
    env = _real_get_env()
    env["root_dir"] = str(ROOT / "state" / "panop")
    env["zotero_api_key"] = secrets.get("zotero.api_key", "") or env.get("zotero_api_key", "")
    env["zotero_user_id"] = secrets.get("zotero.user_id", "") or env.get("zotero_user_id", "")
    return env


panop_main.get_env = _get_env_with_secrets


def _log(event: str, **fields):
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": datetime.now().isoformat(timespec="seconds"), "event": event, **fields}
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _candidate_urls(url: str, item: dict) -> set[str]:
    out = {
        url,
        panop_main.canonicalize_url(url),
        item.get("canonical_url"),
        item.get("original_url"),
    }
    out.discard(None)
    out.discard("")
    return {str(x) for x in out if x}


def _load_zotero_evidence(refresh_api: bool = False):
    try:
        panop_main._load_zotero_url_cache()
        if refresh_api:
            panop_main.refresh_zotero_url_cache()
    except Exception:
        pass
    with panop_main._zotero_url_cache_lock:
        api_urls = set(panop_main._zotero_url_cache.get("urls") or set())
        api_dois = set(panop_main._zotero_url_cache.get("dois") or set())
    local = panop_main._scan_local_zotero_evidence()
    return {
        "urls": api_urls | set(local.get("urls") or set()),
        "dois": api_dois | set(local.get("dois") or set()),
        "local_error": local.get("error"),
    }


def _has_zotero(url: str, item: dict, evidence: dict) -> bool:
    acc = item.get("_accountability") or {}
    if item.get("z_synced") and acc.get("last_event") == "closed_save_repair":
        return True
    doi = (item.get("doi") or "").lower().replace("https://doi.org/", "").strip()
    return bool(_candidate_urls(url, item) & evidence["urls"] or (doi and doi in evidence["dois"]))


def _has_bookmark(url: str, item: dict, bookmarks: set[str]) -> bool:
    return bool(_candidate_urls(url, item) & bookmarks)


def _is_repair_candidate(item: dict) -> bool:
    cat = (item.get("cat_id") or item.get("category") or "").strip().lower()
    return bool(cat and cat != "uncategorized")

def _load_repair_history() -> dict:
    history = panop_main.load_history()
    for p in (ROOT / "state" / "restore_points").glob("*/panop_history.json"):
        try:
            snap = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        for url, item in snap.items():
            key = item.get("canonical_url") or panop_main.canonicalize_url(url) or url
            if key in history or url in history:
                continue
            restored = dict(item)
            restored["restored_from_history_snapshot"] = str(p)
            restored.setdefault("canonical_url", key)
            history[key] = restored
    return history


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="write missing Zotero/bookmark saves")
    ap.add_argument("--limit", type=int, default=0, help="optional max rows for testing")
    ap.add_argument("--sleep", type=float, default=0.15, help="seconds between Zotero writes")
    ap.add_argument("--refresh-api", action="store_true", help="refresh full Zotero API cache before planning")
    args = ap.parse_args(argv)

    env = panop_main.get_env()
    has_zotero_creds = bool((env.get("zotero_api_key") or "").strip() and (env.get("zotero_user_id") or "").strip())
    history = _load_repair_history()
    rows = [(u, it) for u, it in history.items() if _is_repair_candidate(it)]
    if args.limit:
        rows = rows[: args.limit]

    bookmarks = panop_main.scan_chrome_bookmarks_for_panop()
    zotero = _load_zotero_evidence(refresh_api=args.refresh_api)

    planned = []
    counts = Counter()
    for url, item in rows:
        b_ok = _has_bookmark(url, item, bookmarks)
        z_ok = _has_zotero(url, item, zotero)
        if b_ok and z_ok:
            counts["already_verified"] += 1
            continue
        planned.append((url, item, b_ok, z_ok))
        if not b_ok:
            counts["bookmark_missing"] += 1
        if not z_ok:
            counts["zotero_missing"] += 1

    summary = {
        "mode": "commit" if args.commit else "dry_run",
        "history_rows": len(history),
        "candidate_rows": len(rows),
        "already_verified": counts["already_verified"],
        "needs_repair": len(planned),
        "bookmark_missing": counts["bookmark_missing"],
        "zotero_missing": counts["zotero_missing"],
        "has_zotero_credentials": has_zotero_creds,
        "zotero_local_error": zotero.get("local_error"),
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    _log("repair_plan", **summary)

    if not args.commit:
        return 0
    if not has_zotero_creds and counts["zotero_missing"]:
        print("BLOCKED: Zotero credentials are missing, cannot write missing Zotero items.")
        _log("repair_blocked", reason="missing_zotero_credentials", zotero_missing=counts["zotero_missing"])
        return 2

    repaired = Counter()
    for i, (url, item, b_ok, z_ok) in enumerate(planned, 1):
        title = item.get("title") or url
        category = item.get("category") or item.get("cat_id") or "Panop"
        abstract = item.get("abstract") or ""
        doi = item.get("doi") or None
        z_result = z_ok
        b_result = b_ok
        if not z_ok:
            z_result = panop_main.send_to_zotero(url, title, abstract, category, doi=doi)
            if z_result:
                item["z_synced"] = True
                repaired["zotero_written"] += 1
        if not b_ok:
            b_result = panop_main.add_chrome_bookmark(url, title, category)
            if b_result:
                item["b_synced"] = True
                bookmarks |= _candidate_urls(url, item)
                repaired["bookmark_written"] += 1
            else:
                repaired["bookmark_queued_or_failed"] += 1

        source = "repair_panop_closed_saves"
        try:
            panop_main._stamp_accountability(item, url, "closed_save_repair", source)
            panop_main._record_accountability_event(
                "closed_save_repair",
                url,
                item,
                source=source,
                zotero_before=z_ok,
                bookmark_before=b_ok,
                zotero_after=bool(z_result),
                bookmark_after=bool(b_result),
            )
        except Exception:
            pass
        _log(
            "repair_row",
            index=i,
            url=url,
            title=title[:160],
            category=category,
            zotero_before=z_ok,
            bookmark_before=b_ok,
            zotero_after=bool(z_result),
            bookmark_after=bool(b_result),
        )
        if i % 25 == 0:
            panop_main.save_history(history)
            print(f"[{i}/{len(planned)}] {dict(repaired)}")
        time.sleep(args.sleep)

    panop_main.save_history(history)
    print(json.dumps({"done": True, **dict(repaired)}, indent=2, ensure_ascii=False))
    _log("repair_done", **dict(repaired))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
