"""Panop Zotero hygiene — remove junk + duplicates from the Panop collection
tree, REVERSIBLY (Zotero trash, never permanent delete) and only after a full
on-disk backup.

Bruno 2026-06-14: inspecting the live tree showed ~43% duplicates plus hundreds
of Cloudflare/recaptcha/403 interstitials and "Untitled"/bare-domain rows — the
old pipeline saved whatever it fetched and deduped on URL only. The engine gate
(_is_junk_page / _title_dedup_key in panop_server/main.py) stops new junk; this
script cleans what's already there.

Safety:
  • Full backup of every Panop item's raw API JSON before any change.
  • Items are TRASHED (data.deleted=1), recoverable from Zotero's Trash.
  • Dry-run by default; --commit to act.
  • Dedup KEEPS the richest copy (longest abstract, then earliest added).

Creds come from panop_env.json (zotero_api_key / zotero_user_id /
zotero_collection_key), the same source the live server uses.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
BACKUP_DIR = ROOT / "state" / "panop" / "backups"


def _load_creds():
    for p in (ROOT / "panop_env.json", ROOT / "external" / "panop_server" / "panop_env.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8-sig"))
            if d.get("zotero_api_key") and d.get("zotero_user_id"):
                return d["zotero_api_key"], str(d["zotero_user_id"]), d.get("zotero_collection_key", "")
        except Exception:
            continue
    raise SystemExit("No Zotero creds in panop_env.json")


# ── quality gate (kept in lockstep with panop_server/main.py) ───────────────
_JUNK = (
    "just a moment", "checking your browser", "checking your connection",
    "checking if the site connection is secure", "attention required",
    "are you a robot", "verify you are human", "please verify you are",
    "verifying you are human", "recaptcha", "captcha", "ddos protection",
    "access denied", "access to this page has been denied",
    "you have been blocked", "you are being rate limited", "rate limited",
    "bot verification", "human verification", "security check",
    "enable javascript", "javascript is required", "please enable cookies",
    "403 forbidden", "404 not found", "error 404", "error 403",
    "page not found", "page not available", "this page isn", "isn’t available",
    "site can’t be reached", "this site can", "502 bad gateway",
    "503 service", "service unavailable", "too many requests",
    "are you human", "one moment, please", "loading…", "loading...",
)
_PLACEHOLDER = {"", "untitled", "(no title)", "no title", "document", "new tab",
                "redirecting", "redirecting…", "redirect", "loading", "home"}


def _norm(t):
    return re.sub(r"\s+", " ", (t or "").lower().strip())


def _host(u):
    try:
        from urllib.parse import urlparse
        h = (urlparse(u or "").netloc or "").lower()
        return h[4:] if h.startswith("www.") else h
    except Exception:
        return ""


def is_junk(title, url=""):
    t = _norm(title)
    if t in _PLACEHOLDER:
        return "placeholder_title"
    if len(t) <= 2:
        return "title_too_short"
    for p in _JUNK:
        if p in t:
            return "block_or_error_page"
    tn = t[4:] if t.startswith("www.") else t
    if re.fullmatch(r"[a-z0-9][a-z0-9.\-]*\.[a-z]{2,}", tn):
        return "title_is_bare_domain"
    if _host(url) and tn == _host(url):
        return "title_is_domain"
    return None


def title_key(title, url=""):
    t = _norm(title)
    if not t or is_junk(title, url):
        return ""
    return f"{t}@{_host(url)}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="actually trash (default: dry run)")
    ap.add_argument("--sleep", type=float, default=0.4, help="seconds between write batches")
    args = ap.parse_args()

    api_key, uid, root_key = _load_creds()
    H = {"Zotero-API-Key": api_key, "Zotero-API-Version": "3"}
    base = f"https://api.zotero.org/users/{uid}"

    # discover Panop tree collection keys
    if not root_key:
        r = requests.get(f"{base}/collections/top?limit=100", headers=H, timeout=30)
        root_key = next((c["key"] for c in r.json() if c["data"]["name"] == "Panop"), "")
    if not root_key:
        raise SystemExit("Panop collection not found")
    col_keys = [root_key]
    rs = requests.get(f"{base}/collections/{root_key}/collections?limit=50", headers=H, timeout=30)
    if rs.status_code == 200:
        col_keys += [c["key"] for c in rs.json()]

    # gather every top-level item across the tree (dedup by item key)
    items = {}
    for ck in col_keys:
        start = 0
        while True:
            rr = requests.get(f"{base}/collections/{ck}/items/top?limit=100&start={start}",
                              headers=H, timeout=40)
            if rr.status_code != 200:
                break
            batch = rr.json()
            if not batch:
                break
            for it in batch:
                items[it["key"]] = it
            if len(batch) < 100:
                break
            start += len(batch)
    print(f"Panop top-level items: {len(items)}")

    # BACKUP (always, even on dry run)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    backup = BACKUP_DIR / f"zotero_panop_backup_{stamp}.json"
    backup.write_text(json.dumps(list(items.values()), ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"backup -> {backup} ({len(items)} items)")

    # ── canonical-URL helper + "dead URL" test ──────────────────────────────
    def canon(u):
        try:
            from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
            p = urlparse(u or "")
            netloc = (p.netloc or "").lower()
            if netloc.startswith("m."):
                netloc = "www." + netloc[2:]
            path = (p.path or "").rstrip("/")
            return urlunparse(((p.scheme or "https").lower(), netloc, path, "", "", ""))
        except Exception:
            return u or ""

    _DEAD_HOSTS = {"t.co", "lm.facebook.com", "l.facebook.com", "lnkd.in",
                   "news.google.com", "out.reddit.com"}

    def dead_url(u):
        """A URL with NO real article behind it: a bare homepage (no path) or a
        tracking/redirect shim. Such an item is genuinely worthless and safe to
        trash. A real article path is NOT dead — it just needs re-titling."""
        try:
            from urllib.parse import urlparse
            p = urlparse(u or "")
            if (p.netloc or "").lower().lstrip("www.") in _DEAD_HOSTS:
                return True
            if "/url?" in (u or "") or "/l.php" in (u or ""):
                return True
            return len((p.path or "").strip("/")) == 0 and not p.query
        except Exception:
            return False

    # ── classify ────────────────────────────────────────────────────────────
    # 1) dedup by canonical URL (same link, any title) AND by title+host
    #    (same article, different URL). Keep the richest copy.
    # 2) TRASH only: dupe-extras + items that are BOTH junk-titled AND have a
    #    dead URL (no recoverable article).
    # 3) Real-article-but-bad-title items survive and are listed for re-fetch.
    from collections import Counter
    groups = {}
    for k, it in items.items():
        d = it.get("data", {})
        title, url = d.get("title", ""), d.get("url", "")
        cu = canon(url)
        tk = title_key(title, url)
        gkey = ("u:" + cu) if cu else None
        if not gkey:
            gkey = ("t:" + tk) if tk else None
        if not gkey:
            continue
        groups.setdefault(gkey, []).append(it)
        if tk:                       # also link title-key groups to catch
            groups.setdefault("t:" + tk, []).append(it)  # diff-URL same-article

    dupes, seen = {}, set()
    for gkey, grp in groups.items():
        uniq = {it["key"]: it for it in grp}
        if len(uniq) < 2:
            continue
        ordered = sorted(uniq.values(),
                         key=lambda it: (-len((it["data"].get("abstractNote") or "")),
                                         it["data"].get("dateAdded") or ""))
        keep = ordered[0]["key"]
        for extra in ordered[1:]:
            if extra["key"] not in dupes:
                dupes[extra["key"]] = keep

    dead_junk, needs_retitle = {}, {}
    for k, it in items.items():
        if k in dupes:
            continue
        d = it.get("data", {})
        reason = is_junk(d.get("title", ""), d.get("url", ""))
        if not reason:
            continue
        if dead_url(d.get("url", "")):
            dead_junk[k] = reason
        else:
            needs_retitle[k] = reason     # real article, failed title — KEEP

    trash = {**{k: ("dead:" + v) for k, v in dead_junk.items()},
             **{k: ("dupe_of:" + v) for k, v in dupes.items()}}
    print(f"\n  duplicate extras (keep richest):      {len(dupes)}")
    print(f"  dead junk (block page w/ dead URL):   {len(dead_junk)} {dict(Counter(dead_junk.values()))}")
    print(f"  -> TOTAL to trash (reversible):       {len(trash)}")
    print(f"  KEPT, need re-fetch/re-title:         {len(needs_retitle)} {dict(Counter(needs_retitle.values()))}")
    print(f"  clean survivors:                      {len(items) - len(trash)}")
    # persist the retitle worklist for the re-fetch pass
    (BACKUP_DIR / f"zotero_panop_retitle_{stamp}.json").write_text(
        json.dumps({k: {"url": items[k]["data"].get("url"),
                        "title": items[k]["data"].get("title"),
                        "version": items[k]["version"]}
                    for k in needs_retitle}, ensure_ascii=False, indent=1), encoding="utf-8")

    if not args.commit:
        print("\nDRY RUN — re-run with --commit to trash (reversible; in Zotero Trash).")
        for k, why in list(trash.items())[:6]:
            print("   ", why, "::", (items[k]["data"].get("title") or "")[:60], "|", (items[k]["data"].get("url") or "")[:50])
        return

    # COMMIT — batch set deleted=1 (Zotero allows 50 writes/request)
    keys = list(trash)
    trashed = failed = 0
    for i in range(0, len(keys), 50):
        chunk = keys[i:i + 50]
        payload = [{"key": k, "version": items[k]["version"], "deleted": 1} for k in chunk]
        r = requests.post(f"{base}/items", headers={**H, "Content-Type": "application/json"},
                          data=json.dumps(payload), timeout=60)
        if r.status_code in (200, 201):
            body = r.json()
            trashed += len(body.get("successful") or {})
            failed += len(body.get("failed") or {})
        else:
            failed += len(chunk)
            print(f"  batch {i} HTTP {r.status_code}: {r.text[:160]}")
        time.sleep(args.sleep)
    print(f"\nTRASHED {trashed} | failed {failed} (recoverable in Zotero > Trash)")
    (BACKUP_DIR / f"zotero_panop_trashed_{stamp}.json").write_text(
        json.dumps(trash, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
