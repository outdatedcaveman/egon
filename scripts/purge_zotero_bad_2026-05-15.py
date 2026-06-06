"""Delete obviously-wrong Zotero entries added during the 2026-05-15 incident.

Filter: items added 2026-05-15 (UTC any time today before this script runs)
WHOSE host is on the WRONG_LIST or is missing. Keep all other entries.

Lists every match first (DRY RUN by default). To actually delete, pass --commit.
Writes a deletion audit log to logs/zotero-purge-2026-05-15.log.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx

ROOT = Path(__file__).resolve().parent.parent
LOG_FILE = ROOT / "logs" / "zotero-purge-2026-05-15.log"

# Load Zotero credentials via Egon's secrets module
sys.path.insert(0, str(ROOT))
from lib import secrets   # noqa: E402

USER_ID = secrets.get("zotero.user_id")
API_KEY = secrets.get("zotero.api_key")
if not USER_ID or not API_KEY:
    print("Zotero credentials not configured"); sys.exit(1)

BASE = f"https://api.zotero.org/users/{USER_ID}"
HEADERS = {"Zotero-API-Key": API_KEY, "Zotero-API-Version": "3"}

# Hosts that are CLEARLY not academic articles or books — delete from Zotero
# if they were added during the incident window.
WRONG_HOSTS = {
    # General reference / encyclopedia (not papers)
    "en.wikipedia.org", "wikipedia.org",
    # Commerce
    "www.amazon.com", "amazon.com",
    "www.amazon.com.br", "amazon.com.br",
    "www.amazon.es", "www.amazon.co.uk", "www.amazon.de",
    "store.steampowered.com", "ebay.com", "www.ebay.com",
    # Code / tech / consumer
    "github.com", "www.github.com", "gist.github.com",
    "stackoverflow.com", "stackexchange.com",
    "www.xda-developers.com", "xda-developers.com",
    "androidpolice.com", "9to5google.com", "techcrunch.com",
    "theverge.com", "engadget.com", "gizmodo.com",
    # Blog platforms
    "medium.com", "substack.com", "wordpress.com", "blogger.com",
    "tumblr.com",
    # Social / video
    "twitter.com", "x.com", "reddit.com", "www.reddit.com",
    "facebook.com", "www.facebook.com", "instagram.com",
    "youtube.com", "www.youtube.com", "youtu.be",
    "tiktok.com", "linkedin.com", "www.linkedin.com",
    # AI tools (not papers themselves)
    "chat.openai.com", "claude.ai", "chatgpt.com",
    "huggingface.co",  # the model hub itself, not papers
    "paperswithcode.com",  # debatable but usually a catalog page not a paper
    # Local PDFs / news aggregators / shopping
    "news.google.com", "news.yahoo.com",
}

INCIDENT_DAY = "2026-05-15"  # delete only entries added this date


def _log(level: str, **kw):
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    entry = {"ts": datetime.now().isoformat(timespec="seconds"), "level": level, **kw}
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _candidates():
    """Iterate all items added on the incident day with URL host on WRONG_HOSTS."""
    start = 0
    while True:
        with httpx.Client(timeout=30, headers=HEADERS) as client:
            r = client.get(f"{BASE}/items",
                           params={"sort": "dateAdded", "direction": "desc",
                                   "limit": 100, "start": start})
        if r.status_code != 200:
            _log("error", event="api_error", status=r.status_code, body=r.text[:200])
            break
        batch = r.json()
        if not batch:
            break
        stop = False
        for it in batch:
            d = it.get("data", {})
            date_added = (d.get("dateAdded") or "")[:10]
            if date_added < INCIDENT_DAY:
                stop = True
                break
            if date_added != INCIDENT_DAY:
                continue
            url = d.get("url") or ""
            try:
                host = (urlparse(url).hostname or "").lower()
            except Exception:
                host = ""
            if host in WRONG_HOSTS:
                yield it
        start += 100
        if stop:
            break
        time.sleep(0.3)  # be polite


def main():
    commit = "--commit" in sys.argv
    print(f"Mode: {'COMMIT (will delete)' if commit else 'DRY RUN (preview only)'}")
    _log("info", event="purge_start", commit=commit, incident_day=INCIDENT_DAY,
         wrong_hosts_count=len(WRONG_HOSTS))

    cands = list(_candidates())
    print(f"\nFound {len(cands)} candidates to delete:")
    from collections import Counter
    by_host = Counter()
    for it in cands[:30]:
        d = it["data"]
        host = (urlparse(d.get("url","")).hostname or "?")
        by_host[host] += 1
        print(f"  {d.get('key')}  {host:30}  {(d.get('title') or '')[:60]}")
    if len(cands) > 30:
        print(f"  … and {len(cands)-30} more")
    print(f"\nBy host (top 10):")
    for h, n in Counter((urlparse(it['data'].get('url','')).hostname or '?') for it in cands).most_common(10):
        print(f"  {n:>4}  {h}")

    if not commit:
        print("\nDRY RUN — no deletions performed. Re-run with --commit to delete.")
        _log("info", event="dry_run_done", candidate_count=len(cands))
        return 0

    print(f"\nSending {len(cands)} items to Zotero Trash (recoverable from Zotero UI)…")
    trashed = 0
    failed = 0
    # Use PATCH with deleted=1 → sends to Trash (NOT permanent delete).
    # Bruno can restore any of these from Zotero's Trash collection if he wants.
    for it in cands:
        key = it["data"]["key"]
        version = it["data"]["version"]
        try:
            r = httpx.patch(f"{BASE}/items/{key}", timeout=15,
                            headers={**HEADERS, "If-Unmodified-Since-Version": str(version),
                                     "Content-Type": "application/json"},
                            content=json.dumps({"deleted": 1}))
            if r.status_code in (200, 204):
                trashed += 1
                _log("info", event="trashed", key=key, url=it['data'].get('url'))
            else:
                failed += 1
                _log("warn", event="trash_failed", key=key, status=r.status_code, body=r.text[:200])
        except Exception as e:
            failed += 1
            _log("warn", event="trash_exception", key=key, error=str(e)[:200])
        time.sleep(0.15)

    print(f"\nDONE: trashed={trashed} failed={failed}")
    print("→ Items are in Zotero's Trash. Open Zotero → 'Trash' (left sidebar) to review or restore.")
    _log("info", event="purge_done", trashed=trashed, failed=failed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
