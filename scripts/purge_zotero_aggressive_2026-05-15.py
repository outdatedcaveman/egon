"""Aggressive purge — send to Zotero Trash every entry from 2026-05-15 that
was classified by AI-fallback OR by Science News redirect, regardless of host.

Rationale (Bruno, 2026-05-15): "if you saved 1500 refs to Zotero from my phone
yesterday I'm pretty sure way more than 200 are wrong". The first-pass purge
only filtered by host (Wikipedia/Amazon/etc., 242 candidates). This script
adds the per-classifier-path filter: every URL whose history entry shows
`ai_learned: True` or `extracted_from: <url>` is trashed.

This catches:
  - AI-fallback misclassifications regardless of where they ended up
    (a "fake-academic" page that scored high on bag-of-words even if its
    host doesn't appear on the host blocklist)
  - Science News redirects (every redirect was a heuristic guess; the
    "underlying article" we extracted may be unrelated to the press release)

Keeps closed only entries that came from explicit domain_rule matches into
trusted-academic domains.

Sends to Trash (recoverable in Zotero UI), does not hard-delete.
DRY RUN by default; pass --commit to act.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx

ROOT = Path(__file__).resolve().parent.parent
LOG_FILE = ROOT / "logs" / "zotero-purge-aggressive-2026-05-15.log"
HISTORY = ROOT / "state" / "panop" / "panop_history.json"

sys.path.insert(0, str(ROOT))
from lib import secrets   # noqa: E402

USER_ID = secrets.get("zotero.user_id")
API_KEY = secrets.get("zotero.api_key")
if not USER_ID or not API_KEY:
    print("Zotero credentials not configured"); sys.exit(1)

BASE = f"https://api.zotero.org/users/{USER_ID}"
HEADERS = {"Zotero-API-Key": API_KEY, "Zotero-API-Version": "3"}

INCIDENT_DAY = "2026-05-15"


def _log(level: str, **kw):
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    entry = {"ts": datetime.now().isoformat(timespec="seconds"), "level": level, **kw}
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _wrong_urls_from_history() -> set[str]:
    """Build the set of URLs whose history entries indicate likely-wrong saves."""
    h = json.loads(HISTORY.read_text(encoding="utf-8"))
    wrong = set()
    for storage_url, it in h.items():
        if not (it.get("date") or "").startswith(INCIDENT_DAY):
            continue
        # Anything that fired the AI fallback OR was a SciNews redirect
        if it.get("ai_learned") or it.get("extracted_from"):
            wrong.add(storage_url)
            # Some entries had a different canonical URL than the original;
            # include both so we catch the Zotero record regardless of which
            # URL it was saved under.
            cu = it.get("canonical_url")
            ou = it.get("original_url")
            if cu and cu != storage_url: wrong.add(cu)
            if ou and ou != storage_url: wrong.add(ou)
    return wrong


def _all_today_zotero_items():
    """Yield every Zotero item added on 2026-05-15 (paginated)."""
    start = 0
    while True:
        with httpx.Client(timeout=30, headers=HEADERS) as client:
            r = client.get(f"{BASE}/items",
                           params={"sort": "dateAdded", "direction": "desc",
                                   "limit": 100, "start": start})
        if r.status_code != 200: break
        batch = r.json()
        if not batch: break
        stop = False
        for it in batch:
            d = it.get("data", {})
            da = (d.get("dateAdded") or "")[:10]
            if da < INCIDENT_DAY:
                stop = True; break
            if da != INCIDENT_DAY: continue
            # skip already-trashed
            if d.get("deleted"): continue
            yield it
        start += 100
        if stop: break
        time.sleep(0.2)


def main():
    commit = "--commit" in sys.argv
    print(f"Mode: {'COMMIT (will trash)' if commit else 'DRY RUN (preview only)'}")
    wrong = _wrong_urls_from_history()
    _log("info", event="start", commit=commit, wrong_url_count=len(wrong))
    print(f"History flags: {len(wrong)} URLs marked as ai_fallback OR scinews_redirect")

    cands = []
    for it in _all_today_zotero_items():
        url = it["data"].get("url") or ""
        # canonical/raw URLs — match either way
        if url in wrong or _canon(url) in wrong:
            cands.append(it)

    print(f"\nFound {len(cands)} Zotero items to trash:")
    from collections import Counter
    hosts = Counter()
    for it in cands:
        try: hosts[(urlparse(it['data'].get('url','')).hostname or '?').replace('www.','')] += 1
        except: hosts['?'] += 1
    print("Top 12 hosts to trash:")
    for h, n in hosts.most_common(12): print(f"  {n:>4}  {h}")

    if not commit:
        print(f"\nDRY RUN — no deletions. Re-run with --commit to send {len(cands)} items to Trash.")
        return 0

    print(f"\nSending {len(cands)} items to Trash…")
    trashed = 0; failed = 0
    for it in cands:
        key = it["data"]["key"]; version = it["data"]["version"]
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
                _log("warn", event="failed", key=key, status=r.status_code, body=r.text[:200])
        except Exception as e:
            failed += 1
            _log("warn", event="exception", key=key, error=str(e)[:200])
        if trashed % 50 == 0 and trashed > 0:
            print(f"  …{trashed} trashed")
        time.sleep(0.12)
    print(f"\nDONE: trashed={trashed} failed={failed}")
    print("→ All recoverable in Zotero → Trash sidebar.")
    _log("info", event="done", trashed=trashed, failed=failed)
    return 0


def _canon(u):
    """Mirror Panop's canonicalize_url just enough for matching."""
    try:
        from urllib.parse import urlparse, urlunparse
        p = urlparse(u)
        # strip trailing slash, common tracking params
        q = "&".join([s for s in p.query.split("&") if not any(s.startswith(t+"=") for t in ("utm_","fbclid","gclid","mc_eid","mc_cid","_hsenc","_hsmi","ref"))])
        return urlunparse((p.scheme, p.netloc.lower().replace("www.",""), p.path.rstrip("/"), "", q, ""))
    except: return u


if __name__ == "__main__":
    raise SystemExit(main())
