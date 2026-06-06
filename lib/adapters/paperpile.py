"""Paperpile — Playwright-based (no public API for personal plans).

First-time: user clicks "Login to Paperpile" → browser opens → Google Sign-In
flow → state saved. Subsequent syncs run headless and pull references from
Paperpile's internal endpoints (which require the authenticated session).
"""
from __future__ import annotations

import json
from datetime import datetime

from lib import scraper
from lib import secrets
from lib.snapshot_store import latest_snapshot

META = {
    "id": "paperpile",
    "label": "Paperpile (login-based)",
    "icon": "📑",
    "kind": "reference",
    "needs_auth": True,
    "destructive_actions": [],
    "read_only_default": True,
}

LOGIN_URL = "https://paperpile.com/app"


def is_logged_in() -> bool:
    return scraper.is_logged_in("paperpile")


def start_auth_flow() -> dict:
    return scraper.interactive_login(
        "paperpile", LOGIN_URL,
        wait_message="Sign in with Google. Close this window when you see the Paperpile library.",
        wait_url_contains="paperpile.com/app",
        max_wait_seconds=600,
    )


def revoke() -> dict:
    return scraper.revoke("paperpile")


def _export_path() -> str:
    return (secrets.get("paperpile.export_path") or "").strip()


def _normalize_title(title: str) -> str:
    import re
    t = (title or "").lower()
    t = re.sub(r"[^a-z0-9]", "", t)
    return t


def _standardize_date(date_str: str) -> str:
    if not date_str:
        return ""
    date_str = date_str.strip().lower()
    
    import re
    # Check if already ISO format YYYY-MM-DD
    if re.match(r"^\d{4}-\d{2}-\d{2}", date_str):
        return date_str
        
    from datetime import datetime, timedelta
    now = datetime.now()
    if date_str in ("today", "hoje", "now"):
        return now.strftime("%Y-%m-%d %H:%M:%S")
    if date_str in ("yesterday", "ontem"):
        return (now - timedelta(days=1)).strftime("%Y-%m-%d 12:00:00")
        
    months_en = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
    months_pt = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]
    
    day_match = re.search(r"\b\d{1,2}\b", date_str)
    if not day_match:
        return date_str
        
    day = int(day_match.group(0))
    month_idx = None
    for idx, m in enumerate(months_en):
        if m in date_str:
            month_idx = idx + 1
            break
    if month_idx is None:
        for idx, m in enumerate(months_pt):
            if m in date_str:
                month_idx = idx + 1
                break
                
    if month_idx is None:
        pt_full = ["janeiro", "fevereiro", "marco", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"]
        for idx, m in enumerate(pt_full):
            if m in date_str:
                month_idx = idx + 1
                break
                
    if month_idx is not None:
        year = now.year
        try:
            dt = datetime(year, month_idx, day, 12, 0, 0)
            if dt > now:
                dt = datetime(year - 1, month_idx, day, 12, 0, 0)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
            
    return date_str


def _parse_bibtex(text: str) -> list[dict]:
    """Lightweight dependency-free BibTeX parser for Paperpile exports."""
    import re
    from datetime import datetime, timedelta
    refs: list[dict] = []
    seen = set()
    base_time = datetime.now()
    idx = 0
    for m in re.finditer(r"@(\w+)\s*\{\s*([^,]*),", text):
        start = m.end()
        depth, i = 1, start
        while i < len(text) and depth > 0:
            if text[i] == "{": depth += 1
            elif text[i] == "}": depth -= 1
            i += 1
        body = text[start:i - 1]
        e = {}
        for fm in re.finditer(r"(\w+)\s*=\s*(\{(?:[^{}]|\{[^{}]*\})*\}|\"[^\"]*\")",
                               body, flags=re.DOTALL):
            v = fm.group(2).strip()
            if v and v[0] in "{\"":
                v = v[1:-1]
            v = v.replace("{", "").replace("}", "")
            e[fm.group(1).lower()] = re.sub(r"\s+", " ", v).strip()
        
        title = e.get("title", "").strip()
        if not title:
            continue
        norm_title = _normalize_title(title)
        if norm_title in seen:
            continue
        seen.add(norm_title)

        authors = e.get("author", "")
        if authors:
            authors = ", ".join(a.split(",")[0].strip() for a in authors.split(" and "))
            
        added_str = (base_time - timedelta(seconds=idx * 10)).strftime("%Y-%m-%d %H:%M:%S")
        idx += 1

        refs.append({
            "title": title, "authors": authors,
            "year": e.get("year", "")[:6],
            "publication": e.get("journal") or e.get("booktitle") or e.get("publisher", ""),
            "doi": e.get("doi", ""), "url": e.get("url", ""),
            "added": added_str, "tags": e.get("keywords", ""),
            "abstract": (e.get("abstract", "") or "")[:400],
            "_generated_added": True,
        })
    return refs


def _parse_ris(text: str) -> list[dict]:
    """Lightweight RIS parser."""
    from datetime import datetime, timedelta
    refs, cur, authors, kws = [], {}, [], []
    seen = set()
    base_time = datetime.now()
    idx = 0

    def flush():
        nonlocal idx
        title = cur.get("title", "").strip()
        if not title and not authors:
            return
        norm_title = _normalize_title(title)
        if norm_title in seen:
            return
        seen.add(norm_title)

        y2 = cur.get("date_added", "")
        added_str = ""
        if y2:
            import re
            m_date = re.search(r'\b(\d{4})[-/](\d{1,2})[-/](\d{1,2})\b', y2)
            if m_date:
                try:
                    y = int(m_date.group(1))
                    m = int(m_date.group(2))
                    d = int(m_date.group(3))
                    dt = datetime(y, m, d, 12, 0, 0) - timedelta(seconds=idx)
                    added_str = dt.strftime("%Y-%m-%d %H:%M:%S")
                except ValueError:
                    pass
        is_generated = False
        if not added_str:
            added_str = (base_time - timedelta(seconds=idx * 10)).strftime("%Y-%m-%d %H:%M:%S")
            is_generated = True
        idx += 1

        refs.append({
            "title": title, "authors": ", ".join(authors),
            "year": cur.get("year", "")[:6], "publication": cur.get("journal", ""),
            "doi": cur.get("doi", ""), "url": cur.get("url", ""),
            "added": added_str, "tags": "; ".join(kws),
            "abstract": cur.get("abstract", "")[:400],
            "_generated_added": is_generated,
        })

    for line in text.splitlines():
        if len(line) < 6 or line[2:6] != "  - ":
            continue
        tag, val = line[:2], line[6:].strip()
        if tag == "ER":
            flush(); cur, authors, kws = {}, [], []
        elif tag in ("TI", "T1"): cur["title"] = val
        elif tag in ("AU", "A1"): authors.append(val.split(",")[0].strip())
        elif tag == "PY": cur["year"] = val
        elif tag == "Y2": cur["date_added"] = val
        elif tag in ("JO", "JF", "T2"): cur["journal"] = val
        elif tag == "DO": cur["doi"] = val
        elif tag == "UR": cur["url"] = val
        elif tag == "AB": cur["abstract"] = val
        elif tag == "KW": kws.append(val)
    flush()
    return refs


def export_items() -> list[dict]:
    """Parse the configured Paperpile export file (BibTeX/RIS), re-read every
    call so a freshly re-exported file is reflected immediately. This is the
    reliable path: Paperpile's live library loads via Firestore and can't be
    captured. Bruno 2026-05-22."""
    from pathlib import Path
    p = _export_path()
    if not p:
        return []
    fp = Path(p)
    if not fp.exists():
        return []
    try:
        text = fp.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    if fp.suffix.lower() == ".ris" or "TY  - " in text[:500]:
        return _parse_ris(text)
    return _parse_bibtex(text)


def _harvest_items() -> dict | None:
    """Chrome-extension capture. When you visit paperpile.com/app in your
    real Chrome (where you're already signed in), the Egon extension scrapes
    the library rows and POSTs them to Panop. Zero anti-bot exposure."""
    try:
        import httpx as _httpx
        r = _httpx.get("http://127.0.0.1:8000/api/v1/paperpile/library", timeout=2.0)
        if r.status_code != 200:
            return None
        data = r.json()
        if data.get("status") == "ok":
            return data
    except Exception:
        return None
    return None


def live_status() -> dict:
    # Priority 0: export file — the only reliable path (Firestore-backed
    # library isn't capturable). Re-read every call.
    exp = export_items()
    if exp:
        return {"status": "ok", "source": "export_file",
                "count": len(exp),
                "note": f"{len(exp)} refs from export file. Re-export to update."}
    # Priority 1: Chrome extension harvest (uses your real signed-in session)
    harvest = _harvest_items()
    if harvest:
        return {"status": "ok", "source": "chrome_extension",
                "count": harvest.get("count", 0),
                "received_at": harvest.get("received_at"),
                "note": ("Captured via Chrome extension. Visit paperpile.com/app "
                         "in your real Chrome to refresh.")}
    if secrets.get("paperpile.api_key"):
        return {"status": "ok", "source": "workspace-api",
                "note": "Workspace API key set — direct API mode (not yet wired)."}
    # Personal plan WITHOUT Chrome-extension capture: nothing we can do.
    return {"status": "unconfigured",
            "error": ("Two paths:\n"
                      " (a) Open paperpile.com/app in your real Chrome — the Egon "
                      "extension auto-captures your library.\n"
                      " (b) Workspace plan: set paperpile.api_key.\n"
                      "(Playwright path is permanently blocked by reCAPTCHA.)")}


def snapshot() -> dict:
    items: list[dict] = []
    sources = []
    
    # 1. Try export file
    exp = export_items()
    if exp:
        items.extend(exp)
        sources.append("export_file")
        
    # 2. Try Chrome extension harvest
    harvest = _harvest_items()
    if harvest and harvest.get("items"):
        items.extend(harvest["items"])
        sources.append("chrome_extension")
        
    if not items:
        api_key = secrets.get("paperpile.api_key")
        if not api_key:
            return {"status": "unconfigured",
                    "error": "no export file and no Chrome-extension harvest yet"}
        return {"status": "unconfigured",
                "error": "Workspace API key path is not yet implemented in Egon"}
                
    # Deduplicate by normalized title and merge metadata
    seen = {}
    for it in items:
        title = it.get("title", "").strip()
        if not title:
            continue
        k = _normalize_title(title)
        
        # Standardize date
        it["added"] = _standardize_date(it.get("added", ""))
        
        if k not in seen:
            seen[k] = it
        else:
            existing = seen[k]
            # Merge missing fields
            for field in ("authors", "year", "publication", "doi", "url", "tags", "abstract"):
                if not existing.get(field) and it.get(field):
                    existing[field] = it[field]
            
            # Prefer a real (non-generated) added date over a generated one
            existing_gen = existing.get("_generated_added", False)
            new_gen = it.get("_generated_added", False)
            if existing_gen and not new_gen and it.get("added"):
                existing["added"] = it["added"]
                existing["_generated_added"] = False

    deduped = list(seen.values())
            
    # Sort descending by added timestamp (newest-first)
    deduped_sorted = sorted(deduped, key=lambda x: x.get("added", ""), reverse=True)
    
    return {
        "status": "ok",
        "source": "+".join(sources),
        "synced_at": datetime.now().isoformat(),
        "count": len(deduped_sorted),
        "items": deduped_sorted,
    }
    # Legacy Playwright scrape — kept for reference / disabled
    # because Paperpile's DOM uses ephemeral data-id values and the
    # scrape would yield meaningless rows even if reCAPTCHA passed.
    try:
        items: list[dict] = []  # pragma: no cover
        with scraper.browser_context("paperpile", headless=True) as ctx:
            page = ctx.new_page()
            page.goto(LOGIN_URL, wait_until="networkidle", timeout=60_000)
            page.wait_for_selector("[data-id]", timeout=30_000)
            rows = page.query_selector_all("[data-id]")
            for r in rows[:2000]:
                rid = r.get_attribute("data-id") or ""
                title_el = r.query_selector(".title, h3, h2")
                author_el = r.query_selector(".author, .authors")
                year_el = r.query_selector(".year, .date")
                doi_el = r.query_selector("a[href*='doi.org']")
                items.append({
                    "id":     rid,
                    "title":  (title_el.inner_text() if title_el else "").strip()[:300],
                    "authors": (author_el.inner_text() if author_el else "").strip()[:200],
                    "year":   (year_el.inner_text() if year_el else "").strip()[:6],
                    "doi":    (doi_el.get_attribute("href") if doi_el else "") or "",
                })
            page.close()
        return {"status": "ok", "synced_at": datetime.now().isoformat(),
                "count": len(items), "items": items}
    except Exception as e:
        return {"status": "error",
                "error": (f"{type(e).__name__}: {str(e)[:240]}. "
                          "Paperpile's DOM may have changed — selectors need updating.")}


def items(limit: int = 5000) -> list[dict]:
    # Export file wins (full library); fall back to extension harvest.
    exp = export_items()
    if exp:
        return exp[:limit]
    harvest = _harvest_items()
    if harvest and harvest.get("items"):
        return harvest["items"][:limit]
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
