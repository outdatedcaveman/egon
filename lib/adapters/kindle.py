"""Kindle — region-aware adapter.

Amazon has no public Kindle API. We support two paths, in order of
reliability:

  1. **Data-export ZIP (reliable, recommended)** — set
     `kindle.export_path` in egon-config.json to the path of the ZIP
     Amazon emails after a "Request my data" submission. Egon parses
     highlights, notes, and library directly from the export. This works
     for ANY region and ANY library size.

  2. **Browser-session scrape (fragile, region-specific)** — when no
     export ZIP is present, falls back to scraping
     `<base_url>/notebook` using a saved Playwright session. The base URL
     defaults to `read.amazon.com` (US/global) but you can override it
     via `kindle.region` (e.g. "com.br", "co.uk", "de") OR a full
     `kindle.notebook_url`. **Bruno 2026-05-20**: the US domain shows an
     empty library when the account is on Amazon.com.br — every region
     has its own Kindle library and they don't share.

The scrape path is fragile because (a) Amazon's bot defender blocks
Playwright on many regional domains, and (b) the DOM selectors change
between regional rebrandings. Treat it as best-effort; the export ZIP
is what you'd ship to a friend.
"""
from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime
from pathlib import Path

from lib import scraper, secrets
from lib.snapshot_store import latest_snapshot

META = {
    "id": "kindle",
    "label": "Kindle",
    "icon": "📖",
    "kind": "media",
    "needs_auth": True,
    "destructive_actions": [],
    "read_only_default": True,
}


def _notebook_url() -> str:
    """Region-aware NOTEBOOK URL (highlights + notes only — NOT the full
    library). Use this when you want annotations.

    Resolution order:
      1. `kindle.notebook_url` if set (full URL, no transformations)
      2. derived from `kindle.region` (e.g. "com.br" → "ler.amazon.com.br/notebook")
      3. default `read.amazon.com/notebook` (US/global)
    """
    explicit = secrets.get("kindle.notebook_url")
    if explicit:
        return explicit
    region = (secrets.get("kindle.region") or "com").strip().lower()
    if region in ("com", "us", ""):
        return "https://read.amazon.com/notebook"
    if region in ("com.br", "br", "brazil"):
        # Brazil uses ler.amazon.com.br rather than the read.* subdomain.
        return "https://ler.amazon.com.br/notebook"
    # Generic pattern for other regions (co.uk, de, fr, co.jp, etc.)
    return f"https://read.amazon.{region}/notebook"


def _library_url() -> str:
    """Region-aware FULL-LIBRARY URL — the "Content and Devices" content list.

    Bruno 2026-05-20: the previous URL (`/hz/mycd/myx#/home/content/booksAll`)
    only listed items Amazon categorises as "Ebooks". Sideloaded epubs/pdfs
    are categorised as "Documents" and don't appear there. The endpoint that
    shows EVERY item (books, documents, audiobooks, magazines, samples — all
    of it) is the digital-console contentlist with `allcontent`:

        amazon.<tld>/hz/mycd/digital-console/contentlist/allcontent/dateDsc?pageNumber=1

    It's paginated (~25 items per page); the Chrome extension walks every
    page via fetch() from inside the user's session.

    Resolution:
      1. `kindle.library_url` if set
      2. derived from `kindle.region`
      3. default amazon.com (US/global)
    """
    explicit = secrets.get("kindle.library_url")
    if explicit:
        return explicit
    region = (secrets.get("kindle.region") or "com").strip().lower()
    base = "https://www.amazon.com" if region in ("com", "us", "") \
        else f"https://www.amazon.{region}"
    return f"{base}/hz/mycd/digital-console/contentlist/allcontent/dateDsc?pageNumber=1"


LOGIN_URL = _library_url()      # interactive login lands on the FULL library
NOTEBOOK_URL = _notebook_url()  # annotations live here
LIBRARY_URL = _library_url()


def is_logged_in() -> bool:
    return scraper.is_logged_in("kindle")


def start_auth_flow() -> dict:
    return scraper.interactive_login(
        "kindle", LOGIN_URL,
        wait_message="Sign in to Amazon, then close this window when you see your Kindle notebook",
        wait_url_contains=["mycd", "notebook"],
        max_wait_seconds=600,
    )


def revoke() -> dict:
    return scraper.revoke("kindle")


def _export_zip_items() -> list[dict] | None:
    """Parse the Amazon data-export ZIP if `kindle.export_path` is set and
    the file exists. Returns None if the path is absent / unreadable."""
    path_s = secrets.get("kindle.export_path") or ""
    if not path_s:
        return None
    p = Path(path_s)
    if not p.exists():
        return None
    try:
        with zipfile.ZipFile(p) as zf:
            # The exact filename layout varies by export version. We search
            # for the most-commonly-named JSON/CSV in the archive that
            # contains book/highlight data.
            candidates = [n for n in zf.namelist()
                          if any(token in n.lower() for token in
                                 ("kindle.devices", "kindle.library", "kindle.reading",
                                  "kindle.annotations", "kindle.notes",
                                  "your.books", "your.kindle", "personal", "document", "pdoc"))]
            items: list[dict] = []
            for name in candidates:
                with zf.open(name) as f:
                    raw = f.read().decode("utf-8", errors="replace")
                if name.endswith(".json"):
                    try:
                        data = json.loads(raw)
                    except Exception:
                        continue
                    rows = data if isinstance(data, list) else data.get("items") or []
                    for r in rows:
                        title = r.get("Title") or r.get("title") or ""
                        if not title:
                            continue
                        asin = str(r.get("ASIN") or r.get("asin") or r.get("id") or "")
                        if not asin:
                            import hashlib
                            asin = "pdoc_" + hashlib.md5((title + (r.get("Author") or r.get("authors") or "")).encode("utf-8")).hexdigest()[:12]
                        items.append({
                            "id":     asin,
                            "title":  title,
                            "author": r.get("Author") or r.get("authors") or "",
                            "source": name,
                        })
                elif name.endswith(".csv"):
                    import csv
                    reader = csv.DictReader(io.StringIO(raw))
                    for row in reader:
                        # Different export versions use different column names
                        title = (row.get("Title") or row.get("ProductName")
                                 or row.get("title") or "")
                        if not title:
                            continue
                        asin = str(row.get("ASIN", row.get("asin", "")) or "")
                        if not asin:
                            import hashlib
                            asin = "pdoc_" + hashlib.md5((title + (row.get("Author", row.get("authors", "")) or "")).encode("utf-8")).hexdigest()[:12]
                        items.append({
                            "id":     asin,
                            "title":  title,
                            "author": row.get("Author", row.get("authors", "")) or "",
                            "source": name,
                        })
            return items
    except Exception:
        return None


def _harvest_items() -> dict | None:
    """If the Chrome extension has POSTed library data via Panop, read it
    here. The extension fires whenever you visit your Amazon 'Manage Your
    Content and Devices' page in your REAL Chrome — works regardless of
    region, no Playwright involved, no anti-bot to dodge."""
    try:
        import httpx as _httpx
        r = _httpx.get("http://127.0.0.1:8000/api/v1/kindle/library", timeout=2.0)
        if r.status_code != 200:
            return None
        data = r.json()
        if data.get("status") == "ok":
            return data
    except Exception:
        return None
    return None


def live_status() -> dict:
    # Priority 1: Chrome extension harvest (most reliable — uses your real Chrome)
    harvest = _harvest_items()
    if harvest:
        return {"status": "ok", "source": "chrome_extension",
                "count": harvest.get("count", 0),
                "region": secrets.get("kindle.region") or "com",
                "received_at": harvest.get("received_at"),
                "note": (f"Captured via Chrome extension from {harvest.get('url','')}. "
                         f"Visit your Kindle library in your real Chrome to refresh.")}
    # Priority 2: data-export ZIP — region-independent, offline
    items = _export_zip_items()
    if items is not None:
        return {"status": "ok", "source": "data_export_zip",
                "count": len(items), "region": secrets.get("kindle.region") or "com",
                "note": "Parsed from Amazon data-export ZIP"}
    # Priority 3: Playwright session for the configured region
    if not is_logged_in():
        return {"status": "unconfigured",
                "error": (f"No data yet. Three paths:\n"
                          f"  (a) Open {LIBRARY_URL} in your real Chrome — "
                          f"the Egon extension auto-captures your library.\n"
                          f"  (b) Drop your Amazon data-export ZIP at kindle.export_path.\n"
                          f"  (c) Click 'Login to Kindle' — opens Playwright at "
                          f"{NOTEBOOK_URL} (annotations only, not the full library).")}
    return {"status": "ok", "source": "playwright",
            "region": secrets.get("kindle.region") or "com",
            "note": f"Saved login state for {NOTEBOOK_URL}; sync runs headless."}


def snapshot() -> dict:
    harvest = _harvest_items()
    zip_items = _export_zip_items()
    
    if (harvest and harvest.get("items")) or (zip_items is not None and len(zip_items) > 0):
        merged_items = {}
        
        def add_item(it, default_source):
            asin = it.get("asin") or it.get("id") or ""
            title = it.get("title") or it.get("ProductName") or ""
            if not title:
                return
            
            author = it.get("author") or it.get("Author") or ""
            if not asin:
                import hashlib
                asin = "pdoc_" + hashlib.md5((title + author).encode("utf-8")).hexdigest()[:12]
            
            existing = merged_items.get(asin)
            cover = it.get("cover") or ""
            kind = it.get("kind") or ""
            acquired = it.get("acquired") or ""
            source = it.get("source") or default_source
            
            if existing:
                if not existing.get("cover") and cover:
                    existing["cover"] = cover
                if not existing.get("kind") and kind:
                    existing["kind"] = kind
                if not existing.get("acquired") and acquired:
                    existing["acquired"] = acquired
                if source not in existing["source"]:
                    existing["source"] = f"{existing['source']},{source}"
            else:
                merged_items[asin] = {
                    "asin": asin,
                    "title": title,
                    "author": author,
                    "cover": cover,
                    "kind": kind,
                    "acquired": acquired,
                    "source": source
                }

        if harvest and harvest.get("items"):
            for it in harvest["items"]:
                add_item(it, "chrome_extension")
        
        if zip_items:
            for it in zip_items:
                kind = "Personal" if "personal" in it.get("source", "").lower() or "pdoc" in it.get("source", "").lower() else "Ebook"
                normalized_it = {
                    "asin": it.get("id") or it.get("asin") or "",
                    "title": it.get("title") or "",
                    "author": it.get("author") or "",
                    "cover": it.get("cover") or "",
                    "kind": it.get("kind") or kind,
                    "acquired": it.get("acquired") or "",
                    "source": it.get("source") or "data_export_zip"
                }
                add_item(normalized_it, "data_export_zip")
                
        items_list = list(merged_items.values())
        items_list.sort(key=lambda x: (x.get("title") or "").lower())
        
        source_label = "merged"
        if harvest and not zip_items:
            source_label = "chrome_extension"
        elif zip_items and not harvest:
            source_label = "data_export_zip"
            
        return {
            "status": "ok",
            "source": source_label,
            "synced_at": harvest.get("received_at", datetime.now().isoformat()) if harvest else datetime.now().isoformat(),
            "count": len(items_list),
            "items": items_list
        }
        
    if not is_logged_in():
        return {"status": "unconfigured",
                "error": ("no Chrome-extension harvest, no data-export ZIP, "
                          "and not logged in — see live_status() for the three paths.")}
    try:
        with scraper.browser_context("kindle", headless=True) as ctx:
            page = ctx.new_page()
            page.goto(NOTEBOOK_URL, wait_until="domcontentloaded", timeout=60_000)
            # Wait for the books list to load
            page.wait_for_selector("a.kp-notebook-library-each-book", timeout=30_000)

            # Scroll the books list to lazy load all items
            page.evaluate("""
                async () => {
                    const container = document.querySelector('#kp-notebook-library-panel') || 
                                      document.querySelector('.kp-notebook-library') || 
                                      document.querySelector('.kp-notebook-scroller') ||
                                      document.querySelector('a.kp-notebook-library-each-book')?.parentElement;
                    if (!container) return;
                    
                    let lastHeight = container.scrollHeight;
                    let retries = 0;
                    while (retries < 10) {
                        container.scrollTop = container.scrollHeight;
                        await new Promise(r => setTimeout(r, 1000));
                        let newHeight = container.scrollHeight;
                        if (newHeight === lastHeight) {
                            retries++;
                        } else {
                            lastHeight = newHeight;
                            retries = 0;
                        }
                    }
                }
            """)

            books = page.query_selector_all("a.kp-notebook-library-each-book")
            items: list[dict] = []
            for b in books:
                asin = b.get_attribute("id") or ""
                title_el = b.query_selector("h2.kp-notebook-searchable")
                author_el = b.query_selector("p.kp-notebook-searchable")
                cover_el = b.query_selector("img")
                items.append({
                    "id":     asin,
                    "title":  (title_el.inner_text() if title_el else "").strip(),
                    "author": (author_el.inner_text() if author_el else "").strip(),
                    "cover":  cover_el.get_attribute("src") if cover_el else "",
                    "url":    f"{NOTEBOOK_URL}?asin={asin}",
                })
            page.close()
        return {"status": "ok", "synced_at": datetime.now().isoformat(),
                "count": len(items), "items": items}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:240]}"}


def items(limit: int = 100) -> list[dict]:
    snap = latest_snapshot(META["id"])
    return snap.get("items", [])[:limit] if snap and snap.get("status") == "ok" else []


def stats() -> dict:
    snap = latest_snapshot(META["id"])
    if not snap:
        ls = live_status()
        return {"status": ls.get("status", "no-snapshot"), "count": 0, "last_synced": None,
                "error": ls.get("error") or ls.get("note")}
    return {"status": snap.get("status", "ok"), "count": snap.get("count", 0),
            "last_synced": (snap.get("synced_at") or "")[:16]}
