import sys
import subprocess as _sp
if sys.platform == "win32":
    _CREATE_NO_WINDOW = 0x08000000
    _orig_popen_init = _sp.Popen.__init__
    def _silent_popen_init(self, *args, **kwargs):
        flags = kwargs.get("creationflags", 0) | _CREATE_NO_WINDOW
        kwargs["creationflags"] = flags
        si = kwargs.get("startupinfo")
        if si is None:
            si = _sp.STARTUPINFO()
        si.dwFlags |= _sp.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE
        kwargs["startupinfo"] = si
        return _orig_popen_init(self, *args, **kwargs)
    _sp.Popen.__init__ = _silent_popen_init

import os, json, threading, time, urllib.request, zipfile, subprocess, math, csv, re, uuid, hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
from bs4 import BeautifulSoup
import requests
from datetime import datetime
from fastapi import FastAPI, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Dict, Any
import uvicorn
import multiprocessing

try:
    import cloudscraper
    _scraper = cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "windows", "mobile": False})
except Exception:
    _scraper = None

app = FastAPI(title="Panop Backend Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Live sweep status — readable by the UI via /api/v1/status
sweep_status = {
    "last_run": None,
    "adb_connected": False,
    "device_id": None,
    "tabs_seen": 0,
    "tabs_new": 0,
    "tabs_matched": 0,
    "running": False,
    "last_error": None,
    "bookmarks_pending": 0,
    "last_tab_urls": [],
    "last_tab_fetch_at": None,
    "chrome_running": False
}

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,pt;q=0.8",
    "Sec-Ch-Ua": '"Chromium";v="131", "Google Chrome";v="131", "Not=A?Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "DNT": "1",
}

def _http_get(url, timeout=10, stream=False):
    """requests.get with a full browser header set, falling back to cloudscraper
    on 403/503 (Cloudflare/DataDome) or connection errors."""
    try:
        r = requests.get(url, headers=BROWSER_HEADERS, timeout=timeout, stream=stream, allow_redirects=True)
        if r.status_code in (403, 503, 429) and _scraper is not None:
            try:
                r2 = _scraper.get(url, timeout=timeout, allow_redirects=True)
                return r2
            except Exception:
                return r
        return r
    except Exception:
        if _scraper is not None:
            try:
                return _scraper.get(url, timeout=timeout, allow_redirects=True)
            except Exception:
                return None
        return None

ENV_FILE = "panop_env.json"

def get_env():
    if not os.path.exists(ENV_FILE):
        env = {
            "root_dir": "panop_output",
            "interval_hours": 6,
            "catch_uncategorized": False,
            "strict_domain_scan": True,
            "port": 8000,
            "bookmark_folder": "Panop",          # name of Panop subfolder inside Outros Favoritos
            "zotero_api_key": "",                 # Zotero Web API key
            "zotero_user_id": "",                 # Zotero numeric user ID
            "zotero_collection_key": "",          # optional: target collection key
            "close_tabs_after_save": False,       # opt-in: close tab on phone after successful save
            "require_manual_vetting_before_close": True,
            "enable_autonomous_sweep": False,
            "resolve_terminal_redirects": True,
            "chrome_profile": "Default"           # Chrome profile folder name
        }
        with open(ENV_FILE, "w") as f: json.dump(env, f)
        return env
    try:
        # utf-8-sig: tolerate a BOM. A BOM made json.load throw, which silently
        # fell through to the credential-LESS default dict — that is exactly how
        # the live Zotero api_key/user_id went empty and saves began failing.
        # Bruno 2026-06-14.
        with open(ENV_FILE, "r", encoding="utf-8-sig") as f:
            env = json.load(f)
        # Back-fill new keys if missing (upgrade path)
        changed = False
        for k, v in [("bookmark_folder","Panop"),("zotero_api_key",""),("zotero_user_id",""),("zotero_collection_key",""),("close_tabs_after_save", False), ("require_manual_vetting_before_close", True), ("enable_autonomous_sweep", False), ("resolve_terminal_redirects", True), ("chrome_profile", "Default")]:
            if k not in env: env[k] = v; changed = True
        if changed: save_env(env)
        return env
    except:
        return {"root_dir":"panop_output","interval_hours":6,"catch_uncategorized":False,"strict_domain_scan":True,"port":8000,"bookmark_folder":"Panop","zotero_api_key":"","zotero_user_id":"","zotero_collection_key":"","close_tabs_after_save":False,"require_manual_vetting_before_close":True,"enable_autonomous_sweep":False,"resolve_terminal_redirects":True}

def save_env(env):
    with open(ENV_FILE, "w") as f: json.dump(env, f, indent=4)

def _manual_vetting_required(env=None):
    env = env or get_env()
    return bool(env.get("require_manual_vetting_before_close", True))

def OUTPUT_DIR(): return get_env().get("root_dir", "panop_output")
def RIS_DIR(): return os.path.join(OUTPUT_DIR(), "ris")
def EXPORT_DIR(): return os.path.join(OUTPUT_DIR(), "exports")
def CONFIG_FILE(): return os.path.join(OUTPUT_DIR(), "panop_config.json")
def HISTORY_FILE(): return os.path.join(OUTPUT_DIR(), "panop_history.json")
def LEARNING_FILE(): return os.path.join(OUTPUT_DIR(), "panop_ai_profiles.json")

def init_dirs():
    os.makedirs(OUTPUT_DIR(), exist_ok=True)
    os.makedirs(RIS_DIR(), exist_ok=True)
    os.makedirs(EXPORT_DIR(), exist_ok=True)
    config = load_config()
    for cat in config.get("categories", []):
        d = cat.get("dest_folder", cat["name"])
        target = d if os.path.isabs(d) else os.path.join(OUTPUT_DIR(), d)
        os.makedirs(target, exist_ok=True)

DEFAULT_CONFIG = {
    "categories": [
        {
            "id": "articles", "name": "Articles", "dest_folder": "Android Articles",
            "domain_keywords": ["arxiv.org", "nature.com"], "body_required": ["abstract"],
            "body_required_mode": "ALL", "body_forbidden": [], "tab_group": "", "max_age_days": ""
        },
        {
            "id": "books", "name": "Books", "dest_folder": "Android Books",
            "domain_keywords": ["goodreads.com"], "body_required": ["isbn"],
            "body_required_mode": "ANY", "body_forbidden": [], "tab_group": "", "max_age_days": ""
        }
    ],
    "wireless_ips": []
}

def load_json(path, default):
    if not os.path.exists(path): return default
    try:
        with open(path, "r", encoding="utf-8") as f: return json.load(f)
    except: return default

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f: json.dump(data, f, indent=4)

def normalize_title(t):
    """Normalize title for fuzzy matching."""
    if not t: return ""
    return re.sub(r'\s+', ' ', t.lower().strip())


# ── QUALITY GATE ────────────────────────────────────────────────────────────
# Bruno 2026-06-14: the Panop Zotero tree had ~43% duplicates plus hundreds of
# Cloudflare/recaptcha/403 interstitials and "Untitled"/domain-as-title rows —
# the pipeline saved whatever it fetched, with no validity check. NOTHING is
# saved (Zotero or bookmark) unless it passes this gate, and a junked tab is
# left OPEN (never close-eligible, since z/b stay false).
_JUNK_TITLE_SUBSTRINGS = (
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
_PLACEHOLDER_TITLES = {
    "", "untitled", "(no title)", "no title", "document", "new tab",
    "redirecting", "redirecting…", "redirect", "loading", "home",
}

def _is_junk_page(title, url="", abstract=""):
    """Return a short reason string if this page is NOT worth saving (a block
    page / error / contentless extraction), else None. Title-based so it works
    everywhere a save is attempted, with no extra fetch."""
    t = normalize_title(title)
    if t in _PLACEHOLDER_TITLES:
        return "placeholder_title"
    if len(t) <= 2:
        return "title_too_short"
    for pat in _JUNK_TITLE_SUBSTRINGS:
        if pat in t:
            return f"block_or_error_page:{pat}"
    # Title that is just a bare domain (e.g. "www.psypost.org") = failed
    # extraction; never the real article title.
    tn = t[4:] if t.startswith("www.") else t
    if re.fullmatch(r"[a-z0-9][a-z0-9.\-]*\.[a-z]{2,}", tn):
        return "title_is_bare_domain"
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).netloc or "").lower()
        host = host[4:] if host.startswith("www.") else host
        if host and tn == host:
            return "title_is_domain"
    except Exception:
        pass
    return None

def _title_dedup_key(title, url=""):
    """Stable (normalized-title @ host) key so the SAME article saved under two
    different URLs (pre-redirect vs resolved, tracking variants) dedups. Empty
    when the title is junk — junk never dedups, it's just rejected."""
    t = normalize_title(title)
    if not t or _is_junk_page(title, url):
        return ""
    host = ""
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).netloc or "").lower()
        if host.startswith("www."):
            host = host[4:]
    except Exception:
        pass
    return f"{t}@{host}"

TRACKING_PARAMS = {
    'utm_source','utm_medium','utm_campaign','utm_term','utm_content',
    'fbclid','gclid','mc_cid','mc_eid','igshid','_ga','ref','ref_src','ref_url',
    'yclid','msclkid','spm','share','shared','from','source'
}
_REDIRECT_URL_CACHE = {}
_REDIRECT_HOSTS = {
    "doi.org", "dx.doi.org", "t.co", "bit.ly", "tinyurl.com", "lnkd.in",
    "l.facebook.com", "lm.facebook.com", "substack.com", "news.google.com",
    "scholar.google.com", "www.google.com", "google.com", "www.google.com.br",
    "google.com.br",
}

def _expand_wrapped_tab_url(url):
    """Unwrap common mobile/browser redirect URLs before classifying tabs."""
    if not url:
        return url
    try:
        from urllib.parse import parse_qs, unquote, urlparse
        p = urlparse(url)
        host = (p.netloc or "").lower()
        qs = parse_qs(p.query, keep_blank_values=False)
        if host.endswith("google.com") or host.endswith("google.com.br"):
            for key in ("url", "u", "q"):
                for raw in qs.get(key, []):
                    candidate = unquote(raw).strip()
                    if candidate.startswith(("http://", "https://")):
                        return candidate
        if host in {"t.co", "l.facebook.com", "lm.facebook.com"}:
            for key in ("u", "url"):
                for raw in qs.get(key, []):
                    candidate = unquote(raw).strip()
                    if candidate.startswith(("http://", "https://")):
                        return candidate
    except Exception:
        return url
    return url

def _looks_like_redirect_url(url):
    try:
        from urllib.parse import urlparse
        p = urlparse(url)
        host = (p.netloc or "").lower()
        path = (p.path or "").lower()
        if host in _REDIRECT_HOSTS:
            return True
        if host.endswith(".google.com") or host.endswith(".google.com.br"):
            return True
        return any(marker in path for marker in ("/redirect", "/url", "/out", "/link", "/away"))
    except Exception:
        return False

def _resolve_terminal_tab_url(url, env=None):
    """Return the true terminal URL for redirect-like tabs, bounded and cached."""
    expanded = _expand_wrapped_tab_url(url)
    env = env or get_env()
    if not env.get("resolve_terminal_redirects", True):
        return expanded
    if not expanded or not _looks_like_redirect_url(expanded):
        return expanded
    if expanded in _REDIRECT_URL_CACHE:
        return _REDIRECT_URL_CACHE[expanded]
    try:
        resp = requests.head(expanded, allow_redirects=True, timeout=2.5)
        final_url = getattr(resp, "url", None) or expanded
        if final_url == expanded and getattr(resp, "status_code", 0) in (403, 405):
            resp = _http_get(expanded, timeout=3.5)
            final_url = getattr(resp, "url", None) or expanded
        if final_url and final_url.startswith(("http://", "https://")):
            _REDIRECT_URL_CACHE[expanded] = final_url
            return final_url
    except Exception:
        pass
    _REDIRECT_URL_CACHE[expanded] = expanded
    return expanded

def canonicalize_url(url):
    """Collapse common duplicate-URL variants:
    - lowercase scheme/host
    - m./mobile. → www.
    - strip trailing slash
    - drop tracking query params
    - arxiv /pdf/xxx(.pdf) → /abs/xxx
    """
    try:
        from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
        url = _expand_wrapped_tab_url(url)
        p = urlparse(url)
        netloc = (p.netloc or '').lower()
        if netloc.startswith('m.'): netloc = 'www.' + netloc[2:]
        elif netloc.startswith('mobile.'): netloc = 'www.' + netloc[7:]
        path = p.path or ''
        if 'arxiv.org' in netloc and '/pdf/' in path:
            path = path.replace('/pdf/', '/abs/')
            if path.endswith('.pdf'): path = path[:-4]
        if path.endswith('/') and len(path) > 1:
            path = path.rstrip('/')
        qs = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=False)
              if k.lower() not in TRACKING_PARAMS]
        qs.sort()
        return urlunparse(((p.scheme or 'https').lower(), netloc, path, '', urlencode(qs), ''))
    except Exception:
        return url

DOI_RE = re.compile(r'10\.\d{4,9}/[-._;()/:A-Z0-9]+', re.IGNORECASE)

def extract_doi(url, meta_dict=None, text=""):
    """Best-effort DOI extraction. meta_dict: {meta-name: content}."""
    if meta_dict:
        for k in ('citation_doi', 'dc.identifier', 'dc.identifier.doi', 'doi', 'prism.doi'):
            v = meta_dict.get(k)
            if v:
                m = DOI_RE.search(v)
                if m: return m.group(0).rstrip('.,);').lower()
    m = DOI_RE.search(url or '')
    if m: return m.group(0).rstrip('.,);').lower()
    if text:
        m = DOI_RE.search(text[:20000])
        if m: return m.group(0).rstrip('.,);').lower()
    return None

def merge_entries(old, new):
    """Lossless merge of two dictionary entries. Favors more detailed data."""
    merged = old.copy()
    for field in ["abstract", "category", "cat_id", "date", "source", "author", "doi", "canonical_url"]:
        if not merged.get(field) and new.get(field):
            merged[field] = new[field]
    # Longer abstract wins
    if new.get("abstract") and len(new["abstract"]) > len(merged.get("abstract") or ""):
        merged["abstract"] = new["abstract"]
    # URL preference: prefer /abs/ over /pdf/ (arxiv) and https over http
    old_u, new_u = merged.get("url", ""), new.get("url", "")
    if "/pdf/" in old_u and "/abs/" in new_u:
        merged["url"] = new_u
    elif old_u.startswith("http://") and new_u.startswith("https://"):
        merged["url"] = new_u
    # Category preference: prefer specific category over 'uncategorized'
    if merged.get("cat_id") == "uncategorized" and new.get("cat_id") not in (None, "uncategorized"):
        merged["cat_id"] = new["cat_id"]
        merged["category"] = new["category"]
    # OR together the sync flags — if either copy was synced, the merged record is synced
    for flag in ("z_synced", "b_synced"):
        if new.get(flag): merged[flag] = True
    return merged

def consolidate_history():
    """Merge duplicate history entries. Matches on (in order):
        1. DOI (most reliable — cross-publisher, cross-URL)
        2. Canonical URL (same resource via mobile/www, tracking params, etc.)
        3. Normalized title (last-resort fuzzy match)
    """
    h = load_history()
    by_doi = {}
    by_canon = {}
    by_title = {}
    to_delete = []

    for url, item in list(h.items()):
        doi = (item.get("doi") or "").lower().strip()
        canon = canonicalize_url(url)
        title = normalize_title(item.get("title"))
        bad_title = (not title) or title in {"untitled", "untitled pdf", "loading..."}

        winner = None
        if doi and doi in by_doi:
            winner = by_doi[doi]
        elif canon and canon in by_canon and by_canon[canon] != url:
            winner = by_canon[canon]
        elif (not bad_title) and title in by_title:
            winner = by_title[title]

        if winner and winner != url:
            h[winner] = merge_entries(h[winner], item)
            to_delete.append(url)
            # Keep indexes pointing at the survivor
            if doi: by_doi[doi] = winner
            by_canon[canon] = winner
            if not bad_title: by_title[title] = winner
            # Prefer arxiv /abs/ as the canonical key
            if "/abs/" in url and "/pdf/" in winner:
                h[url] = h.pop(winner)
                if doi: by_doi[doi] = url
                by_canon[canon] = url
                if not bad_title: by_title[title] = url
            continue

        if doi: by_doi[doi] = url
        by_canon[canon] = url
        if not bad_title: by_title[title] = url

    if to_delete:
        for url in to_delete:
            if url in h: del h[url]
        save_history(h)
    return len(to_delete)

def load_config():
    if not os.path.exists(CONFIG_FILE()):
        os.makedirs(OUTPUT_DIR(), exist_ok=True)
        save_json(CONFIG_FILE(), DEFAULT_CONFIG)
        return DEFAULT_CONFIG
    return load_json(CONFIG_FILE(), DEFAULT_CONFIG)

def load_history(): return load_json(HISTORY_FILE(), {})

history_lock = threading.Lock()

def save_history(h):
    with history_lock:
        save_json(HISTORY_FILE(), h)

accountability_lock = threading.Lock()

def _accountability_id(url, item=None):
    item = item or {}
    key = canonicalize_url(url or item.get("canonical_url") or item.get("original_url") or "") or url or ""
    return hashlib.sha256(key.encode("utf-8", errors="replace")).hexdigest()[:24]

def _structurally_close_eligible(item):
    if not item:
        return False
    cat_id = (item.get("cat_id") or "").strip().lower()
    if not cat_id or cat_id == "uncategorized":
        return False
    return bool(item.get("z_synced")) and bool(item.get("b_synced"))

def _sync_state(ok):
    return "confirmed" if ok else "pending_or_failed"

def _url_evidence_candidates(url, item=None):
    item = item or {}
    cands = {
        url,
        canonicalize_url(url),
        item.get("canonical_url"),
        item.get("original_url"),
    }
    cands.discard(None)
    cands.discard("")
    return {c for c in cands if c}

def _verify_bookmark_evidence(url, item=None):
    cands = _url_evidence_candidates(url, item)
    seen = scan_chrome_bookmarks_for_panop()
    return bool(cands & seen), sorted(cands & seen)

def _scan_local_zotero_evidence():
    """Read-only local Zotero DB scan. Used only as proof before closing tabs."""
    profile = os.environ.get("USERPROFILE")
    if not profile:
        return {"urls": set(), "dois": set(), "error": "USERPROFILE missing"}
    db = os.path.join(profile, "Zotero", "zotero.sqlite")
    if not os.path.exists(db):
        return {"urls": set(), "dois": set(), "error": "zotero.sqlite not found"}
    urls, dois = set(), set()
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        cur = con.cursor()
        cur.execute(
            "SELECT f.fieldName, v.value "
            "FROM itemData d "
            "JOIN fields f ON f.fieldID=d.fieldID "
            "JOIN itemDataValues v ON v.valueID=d.valueID "
            "WHERE lower(f.fieldName) IN ('url','doi')"
        )
        for field, value in cur.fetchall():
            if not value:
                continue
            if field.lower() == "url":
                urls.add(value)
                urls.add(canonicalize_url(value))
            elif field.lower() == "doi":
                dois.add(str(value).lower().replace("https://doi.org/", "").strip())
        con.close()
        return {"urls": urls, "dois": dois, "error": None}
    except Exception as e:
        return {"urls": urls, "dois": dois, "error": str(e)[:200]}

def _verify_zotero_evidence(url, item=None):
    item = item or {}
    # Refresh API cache first when credentials exist. If credentials are absent,
    # the local DB proof below still protects close decisions.
    try:
        refresh_zotero_url_cache()
    except Exception:
        pass
    cands = _url_evidence_candidates(url, item)
    doi = (item.get("doi") or "").lower().replace("https://doi.org/", "").strip()
    with _zotero_url_cache_lock:
        zurls = set(_zotero_url_cache.get("urls") or set())
        zdois = set(_zotero_url_cache.get("dois") or set())
    api_matches = sorted(cands & zurls)
    if api_matches or (doi and doi in zdois):
        return True, {"source": "zotero_api_cache", "matches": api_matches, "doi_match": bool(doi and doi in zdois)}
    local = _scan_local_zotero_evidence()
    local_matches = sorted(cands & local.get("urls", set()))
    local_dois = local.get("dois", set())
    if local_matches or (doi and doi in local_dois):
        return True, {"source": "zotero_local_db", "matches": local_matches, "doi_match": bool(doi and doi in local_dois)}
    return False, {"source": "zotero_api_cache+local_db", "matches": [], "doi_match": False, "local_error": local.get("error")}

def _verify_close_backups(url, item=None):
    bookmark_ok, bookmark_matches = _verify_bookmark_evidence(url, item)
    zotero_ok, zotero_detail = _verify_zotero_evidence(url, item)
    return {
        "bookmark_ok": bookmark_ok,
        "bookmark_matches": bookmark_matches,
        "zotero_ok": zotero_ok,
        "zotero": zotero_detail,
        "ok": bool(bookmark_ok and zotero_ok),
    }

def _stamp_accountability(item, url, event, source, classification=None, sync=None, close=None):
    """Embed current provenance/accountability on a history row."""
    now = datetime.now().isoformat()
    acc = dict(item.get("_accountability") or {})
    acc.setdefault("id", _accountability_id(url, item))
    acc.setdefault("created_at", item.get("date") or now)
    acc["updated_at"] = now
    acc["last_event"] = event
    acc["source"] = source
    acc["canonical_url"] = item.get("canonical_url") or canonicalize_url(url) or url
    acc["original_url"] = item.get("original_url")
    acc["history_url"] = url
    acc["classification"] = classification or acc.get("classification") or {
        "cat_id": item.get("cat_id"),
        "category": item.get("category"),
        "source": "history_backfill",
        "confidence": None,
        "reason": "Existing history row; original classifier evidence was not recorded.",
    }
    acc["sync"] = sync or {
        "zotero": {"status": _sync_state(item.get("z_synced")), "flag": bool(item.get("z_synced"))},
        "bookmark": {"status": _sync_state(item.get("b_synced")), "flag": bool(item.get("b_synced"))},
    }
    acc["safety"] = {
        "uncategorized_never_close": True,
        "structurally_close_eligible": _structurally_close_eligible(item),
        "manual_vetting_required": bool(_manual_vetting_required()),
        "can_close_now": bool(_safe_to_close(item)),
    }
    if close is not None:
        acc["close"] = close
    item["_accountability"] = acc
    return item

def _record_accountability_event(event, url, item=None, **details):
    item = item or {}
    entry = {
        "ts": datetime.now().isoformat(),
        "event": event,
        "accountability_id": (item.get("_accountability") or {}).get("id") or _accountability_id(url, item),
        "url": url,
        "canonical_url": item.get("canonical_url") or canonicalize_url(url) or url,
        "title": item.get("title"),
        "cat_id": item.get("cat_id"),
        "category": item.get("category"),
        "z_synced": bool(item.get("z_synced")),
        "b_synced": bool(item.get("b_synced")),
        "details": details,
    }
    try:
        path = ACCOUNTABILITY_FILE()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with accountability_lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return entry

def load_profiles(): return load_json(LEARNING_FILE(), {})
def save_profiles(p): save_json(LEARNING_FILE(), p)

STOP_WORDS = {
    "about", "above", "after", "again", "against", "along", "already", "also", "although", "among",
    "around", "because", "been", "before", "being", "below", "between", "both", "could", "didnt",
    "does", "doing", "dont", "down", "during", "each", "either", "even", "every", "first", "from",
    "further", "great", "have", "here", "hers", "herself", "himself", "html", "http", "https",
    "into", "itself", "just", "like", "many", "more", "most", "much", "must", "myself", "neither",
    "never", "obtained", "once", "only", "other", "others", "ours", "ourselves", "over", "same",
    "should", "since", "some", "still", "such", "than", "that", "their", "them", "themselves",
    "then", "there", "these", "they", "this", "those", "through", "today", "under", "until",
    "upon", "very", "wasnt", "were", "what", "when", "where", "which", "while", "whoever",
    "whom", "whose", "why", "with", "within", "without", "would", "yours", "yourself", "yourselves"
}

def get_words(text):
    return [w for w in "".join([c if c.isalnum() else " " for c in text.lower()]).split() if len(w) > 3 and w not in STOP_WORDS]

def update_ai_profile(category_id, text):
    profiles = load_profiles()
    if category_id not in profiles: profiles[category_id] = {}
    words = get_words(text)
    for w in words: profiles[category_id][w] = profiles[category_id].get(w, 0) + 1
    save_profiles(profiles)

def get_ai_prediction(text):
    profiles = load_profiles()
    if not profiles: return None
    
    lower_text = text.lower()
    ban_words = [
        "wikipedia", "amazon", "github", "medium.com", "bbc.com", 
        "reddit.com", "twitter.com", "linkedin.com", "youtube.com", 
        "netflix.com", "stackoverflow.com", "superuser.com", "hacker news"
    ]
    for ban in ban_words:
        if ban in lower_text:
            return None
            
    words = get_words(text)
    if not words: return None
    
    academic_indicators = {"arxiv", "arxivlabs", "doi", "journal", "citation", "citations", "pmid", "abstract", "author", "authors", "volume", "issue", "published", "press", "university"}
    inds = sum(1 for w in words if w in academic_indicators)
    if inds < 3:
        return None
        
    scores = {}
    for cat_id, profile in profiles.items():
        score = sum(profile.get(w, 0) for w in words)
        if score > 0: scores[cat_id] = score
    if not scores: return None
    best_cat = max(scores, key=scores.get)
    if scores[best_cat] > 20: return best_cat
    return None

def ensure_adb():
    adb_dir = os.path.join(OUTPUT_DIR(), "platform-tools")
    adb_exe = os.path.join(adb_dir, "platform-tools", "adb.exe")
    if not os.path.exists(adb_exe):
        zip_path = os.path.join(OUTPUT_DIR(), "tools.zip")
        urllib.request.urlretrieve("https://dl.google.com/android/repository/platform-tools-latest-windows.zip", zip_path)
        with zipfile.ZipFile(zip_path, 'r') as z: z.extractall(adb_dir)
        os.remove(zip_path)
    return adb_exe

def fetch_page_content(url):
    """Returns metadata dict. On network failure returns None.
    Caps response at 200KB to prevent huge pages from bloating memory.
    Uses a full browser header set + cloudscraper fallback to get past
    Cloudflare/DataDome on common academic sites.
    """
    try:
        resp = _http_get(url, timeout=12, stream=True)
        if resp is None or getattr(resp, 'status_code', 0) != 200:
            return None
        # Read at most 200 KB — enough for title + abstract
        raw = b""
        try:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    raw += chunk
                if len(raw) > 200_000:
                    break
        except Exception:
            raw = resp.content[:200_000] if hasattr(resp, 'content') else b""
        html = raw.decode("utf-8", errors="ignore")
        soup = BeautifulSoup(html, 'html.parser')
        metadata = {}
        metadata["canonical_url"] = getattr(resp, 'url', url)
        text = soup.get_text(" ", strip=True)
        metadata["text"] = text[:50_000].lower()
        # Collect relevant meta tags (name + property variants)
        meta_dict = {}
        for m in soup.find_all("meta"):
            key = (m.get("name") or m.get("property") or "").lower()
            if key and m.get("content"):
                meta_dict[key] = m["content"]
        metadata["_meta"] = meta_dict
        t = meta_dict.get("citation_title") or meta_dict.get("og:title") or meta_dict.get("dc.title")
        if not t and soup.title and soup.title.string:
            t = soup.title.string
        metadata["title"] = (t or "").strip()
        ab = (meta_dict.get("citation_abstract") or meta_dict.get("description")
              or meta_dict.get("og:description") or meta_dict.get("dc.description"))
        if ab: metadata["abstract"] = ab.strip()
        author = meta_dict.get("citation_author") or meta_dict.get("author") or meta_dict.get("dc.creator")
        if author: metadata["author"] = author
        metadata["doi"] = extract_doi(url, meta_dict, text)
        # Prefer canonical link if present
        link_can = soup.find("link", rel="canonical")
        if link_can and link_can.get("href"):
            metadata["canonical_url"] = link_can["href"]
        del soup, html, raw, text
        return metadata
    except Exception:
        return None

def _extract_primary_links(url, max_links=50):
    """Science-News 2nd stage, part 1: for a DIGEST / roundup / index page,
    return the candidate primary-article links [{url, title}] it points to —
    outbound anchors whose text reads like a headline and whose URL looks like
    an article. Bruno 2026-06-14: Science News aggregators must explode into
    their contained articles/books, which then get classified + saved to the
    right destinations (not the digest page itself)."""
    try:
        resp = _http_get(url, timeout=12)
        if resp is None or getattr(resp, "status_code", 0) != 200:
            return []
        soup = BeautifulSoup((resp.text or "")[:600_000], "html.parser")
    except Exception:
        return []
    from urllib.parse import urljoin, urlparse
    _SKIP = ("/tag/", "/tags/", "/category", "/categories", "/author/", "/about",
             "/subscribe", "/login", "/signin", "/sign-in", "/register", "/privacy",
             "/terms", "/contact", "/feed", "/rss", "mailto:", "/search", "/account",
             "twitter.com", "x.com", "facebook.com", "instagram.com", "linkedin.com",
             "youtube.com", "/page/", "/comments", "#", "/donate", "/newsletter")
    seen, out = set(), []
    for a in soup.find_all("a", href=True):
        href = urljoin(url, a["href"])
        p = urlparse(href)
        if p.scheme not in ("http", "https"):
            continue
        anchor = a.get_text(" ", strip=True)
        if len(anchor) < 25:                       # headlines are substantial
            continue
        low = href.lower()
        if any(s in low for s in _SKIP):
            continue
        path = (p.path or "").strip("/")
        last = path.split("/")[-1] if path else ""
        # article-like: nested path OR a hyphenated slug (substack/news style)
        if path.count("/") < 1 and "-" not in last:
            continue
        key = canonicalize_url(href)
        if key in seen:
            continue
        seen.add(key)
        out.append({"url": href, "title": anchor[:200]})
        if len(out) >= max_links:
            break
    return out


def is_science_news_aggregator(url, metadata=None):
    """Detect a digest/roundup/index whose value is the links it contains.
    A page qualifies if it surfaces several distinct primary-article links."""
    links = _extract_primary_links(url, max_links=12)
    return len(links) >= 5, links


def second_stage_extract(agg_url, env=None, categories=None, history=None):
    """Science-News 2nd stage, part 2: fetch the aggregator, extract its primary
    links, classify EACH, and save it to its own destination (Articles/Books/
    Science News + bookmark) stamping `extracted_from` provenance. Returns a
    summary.

    `history`: when the caller already holds the in-memory history dict (the
    sweep/drain does), pass it so children are written into the SAME dict and
    the caller persists once — otherwise we'd load a fresh copy, save it, and
    the caller's later save_history() would clobber the children (the classic
    stale-history bug). When None we load+save our own (standalone/retro use)."""
    env = env or get_env()
    categories = categories or (load_config() or {}).get("categories", [])
    links = _extract_primary_links(agg_url)
    res = {"aggregator": agg_url, "found": len(links), "saved": 0,
           "by_cat": {}, "items": []}
    own = history is None
    h = load_history() if own else history
    for ln in links:
        link_url = ln["url"]
        meta = fetch_page_content(link_url) or {}
        if not meta.get("title"):
            meta["title"] = ln["title"]
        cat = _classify_tab_candidate(link_url, meta, categories, env)
        if not cat or cat.get("id") in (None, "", "uncategorized"):
            continue
        term = _resolve_terminal_tab_url(link_url, env)
        storage_url = canonicalize_url(term) or term or link_url
        if storage_url in h:                       # already have it
            continue
        title = meta.get("title") or ln["title"]
        doi = meta.get("doi")
        z = send_to_zotero(storage_url, title, meta.get("abstract", ""), cat["name"], doi=doi)
        b = add_chrome_bookmark(storage_url, title, cat["name"])
        h[storage_url] = {
            "title": title, "category": cat["name"], "cat_id": cat["id"],
            "date": datetime.now().isoformat(), "abstract": meta.get("abstract", ""),
            "canonical_url": meta.get("canonical_url", storage_url),
            "doi": doi, "extracted_from": agg_url,
            "z_synced": z, "b_synced": b, "ai_learned": False,
        }
        _stamp_accountability(h[storage_url], storage_url, "second_stage_extract",
                              f"aggregator:{agg_url}")
        res["by_cat"][cat["id"]] = res["by_cat"].get(cat["id"], 0) + 1
        res["saved"] += 1
        res["items"].append({"url": storage_url, "category": cat["id"], "z": z, "b": b})
    if res["saved"] and own:
        save_history(h)
    return res


def get_pdf_title(url, tab_title=""):
    """Best-effort title resolution for PDF URLs.
    1. Use DevTools tab title if Chrome already resolved it.
    2. For arxiv: fetch the /abs/ page and extract the H1 title.
    3. Fallback: clean up the filename from the URL path.
    """
    if tab_title and tab_title.strip().lower() not in ("", "untitled", "loading..."):
        return tab_title.strip()

    url_lower = url.lower()

    # arxiv special case: swap /pdf/ → /abs/ to get real paper title
    if "arxiv.org/pdf/" in url_lower or "arxiv.org/e-print/" in url_lower:
        try:
            abs_url = url.replace("/pdf/", "/abs/").replace("/e-print/", "/abs/")
            abs_url = abs_url.split("?")[0].rstrip(".pdf")
            resp = _http_get(abs_url, timeout=10)
            if resp is not None and resp.status_code == 200:
                soup = BeautifulSoup(resp.text, 'html.parser')
                h1 = soup.find("h1", class_="title")
                if h1:
                    # arxiv wraps "Title:" in a span inside the h1 — strip it
                    for span in h1.find_all("span"): span.decompose()
                    return h1.get_text(strip=True)
        except Exception:
            pass

    # Generic: derive a readable name from the URL path
    try:
        from urllib.parse import urlparse, unquote
        path = unquote(urlparse(url).path)
        name = path.rstrip("/").split("/")[-1]
        name = name.rsplit(".", 1)[0]  # strip extension
        name = name.replace("-", " ").replace("_", " ").strip()
        if name:
            return name
    except Exception:
        pass

    return "Untitled PDF"

def is_chrome_running():
    """True if any chrome.exe process is live on this machine."""
    try:
        import psutil
        for p in psutil.process_iter(attrs=['name']):
            n = (p.info.get('name') or '').lower()
            if n in ('chrome.exe', 'chrome'):
                return True
    except Exception:
        pass
    return False

def PENDING_BOOKMARKS_FILE(): return os.path.join(OUTPUT_DIR(), "panop_pending_bookmarks.json")
def CLOSE_AUDIT_FILE(): return os.path.join(OUTPUT_DIR(), "panop_close_audit.jsonl")
def ACCOUNTABILITY_FILE(): return os.path.join(OUTPUT_DIR(), "panop_accountability.jsonl")

bookmarks_lock = threading.RLock()

def _queue_bookmark(url, title, category_name):
    with bookmarks_lock:
        q = load_json(PENDING_BOOKMARKS_FILE(), [])
        if not any(x.get('url') == url and x.get('category') == category_name for x in q):
            q.append({
                'url': url, 'title': title, 'category': category_name,
                'queued_at': datetime.now().isoformat()
            })
            save_json(PENDING_BOOKMARKS_FILE(), q)

def _new_guid(): return str(uuid.uuid4())

def _write_bookmark_now(url, title, category_name):
    """Direct file write — ONLY safe when Chrome is not running.
    Returns True on success.
    """
    env = get_env()
    profile_name = env.get("chrome_profile", "Default") or "Default"
    profile = os.environ.get("USERPROFILE")
    if not profile: return False
    udir = os.path.join(profile, "AppData", "Local", "Google", "Chrome", "User Data", profile_name)
    book_path = os.path.join(udir, "Bookmarks")
    if not os.path.exists(book_path): return False
    try:
        with open(book_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        other = data.setdefault("roots", {}).setdefault("other", {})
        other.setdefault("children", [])
        panop_folder_name = env.get("bookmark_folder", "Panop") or "Panop"

        def _stamp(): return str(int(time.time() * 1000000))

        panop_folder = next(
            (c for c in other["children"]
             if c.get("type") == "folder" and c.get("name", "") == panop_folder_name),
            None
        )
        if not panop_folder:
            panop_folder = {"children": [], "date_added": _stamp(), "date_last_used": "0",
                            "guid": _new_guid(), "name": panop_folder_name, "type": "folder"}
            other["children"].append(panop_folder)
        elif not panop_folder.get("guid"):
            panop_folder["guid"] = _new_guid()

        cat_folder = next(
            (c for c in panop_folder["children"]
             if c.get("type") == "folder" and c.get("name", "").lower() == category_name.lower()),
            None
        )
        if not cat_folder:
            cat_folder = {"children": [], "date_added": _stamp(), "date_last_used": "0",
                          "guid": _new_guid(), "name": category_name, "type": "folder"}
            panop_folder["children"].append(cat_folder)
        elif not cat_folder.get("guid"):
            cat_folder["guid"] = _new_guid()

        existing = next((c for c in cat_folder.get("children", [])
                         if c.get("url") == url), None)
        if existing:
            # Update title if incoming is strictly better (non-generic & longer).
            cur = (existing.get("name") or "").strip()
            new = (title or "").strip()
            if new and new.lower() not in _GENERIC_TITLES and (
                cur.lower() in _GENERIC_TITLES or len(new) > len(cur) + 3
            ):
                existing["name"] = new
            else:
                return True  # already present, nothing to improve
        else:
            cat_folder["children"].append({
                "date_added": _stamp(), "date_last_used": "0", "guid": _new_guid(),
                "name": title or url, "type": "url", "url": url
            })
        # Chrome verifies a checksum; removing it (plus the .bak) forces Chrome to
        # recompute rather than revert on next startup.
        data.pop("checksum", None)
        tmp = book_path + ".panop.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, book_path)
        bak = book_path + ".bak"
        if os.path.exists(bak):
            try: os.remove(bak)
            except Exception: pass
        return True
    except Exception:
        return False

def scan_chrome_bookmarks_for_panop():
    """Walks the user's Chrome Bookmarks file and returns every URL present
    inside the Panop folder tree under 'Outros favoritos'. Used to flip
    b_synced=true on local history rows whose bookmark already exists —
    prevents re-queuing duplicates on the next bulk sync.
    Read-only; never mutates the Bookmarks file.
    """
    env = get_env()
    profile_name = env.get("chrome_profile", "Default") or "Default"
    panop_folder_name = env.get("bookmark_folder", "Panop") or "Panop"
    profile = os.environ.get("USERPROFILE")
    if not profile: return set()
    book_path = os.path.join(profile, "AppData", "Local", "Google", "Chrome", "User Data", profile_name, "Bookmarks")
    if not os.path.exists(book_path): return set()
    try:
        with open(book_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return set()
    other = (data.get("roots") or {}).get("other") or {}
    panop = next(
        (c for c in other.get("children", [])
         if c.get("type") == "folder" and c.get("name") == panop_folder_name),
        None
    )
    if not panop: return set()
    urls = set()
    def walk(node):
        if node.get("type") == "url" and node.get("url"):
            urls.add(node["url"])
            urls.add(canonicalize_url(node["url"]))
        for ch in node.get("children") or []:
            walk(ch)
    walk(panop)
    return urls

def reconcile_all():
    """Synchronises local history flags with the actual state of Zotero and
    Chrome bookmarks. Call before bulk sync to avoid duplicate POSTs/queues,
    and on startup to recover from mid-sweep crashes.

    Returns dict: {zotero_matches, bookmark_matches, zotero_url_cache_size, merged_dupes}
    """
    out = {"zotero_matches": 0, "bookmark_matches": 0, "zotero_url_cache_size": 0, "merged_dupes": 0}
    # 1. Refresh Zotero cache from API
    out["zotero_url_cache_size"] = refresh_zotero_url_cache()
    # 2. Scan Chrome bookmarks
    bk_urls = scan_chrome_bookmarks_for_panop()
    # 3. Flip flags on any local rows whose remote copy already exists
    h = load_history()
    changed = False
    with _zotero_url_cache_lock:
        zurls = set(_zotero_url_cache["urls"])
        zdois = set(_zotero_url_cache["dois"])
    for u, item in h.items():
        canon = canonicalize_url(u)
        cands = {u, canon, item.get("canonical_url"), item.get("original_url")}
        cands.discard(None)
        doi = (item.get("doi") or "").lower()
        if not item.get("z_synced") and (cands & zurls or (doi and doi in zdois)):
            item["z_synced"] = True
            out["zotero_matches"] += 1
            changed = True
        if not item.get("b_synced") and (cands & bk_urls):
            item["b_synced"] = True
            out["bookmark_matches"] += 1
            changed = True
    if changed:
        save_history(h)
    # 4. Merge any title/DOI/canonical-URL dupes in history
    out["merged_dupes"] = consolidate_history()
    return out

def add_chrome_bookmark(url, title, category_name):
    """Public entry point used by the sweep / drain.
    Returns True only when the bookmark is known to exist. If Chrome is
    running, queue it for the extension and keep b_synced=false until ACK or
    reconciliation proves Chrome actually has the bookmark.
    """
    # QUALITY GATE — mirror of the Zotero gate; never bookmark a block/error
    # page or a contentless extraction. Bruno 2026-06-14.
    junk = _is_junk_page(title, url)
    if junk:
        try:
            _record_accountability_event("save_blocked_junk", url,
                                         {"title": title}, target="bookmark", reason=junk)
        except Exception:
            pass
        return False
    with bookmarks_lock:
        if is_chrome_running():
            _queue_bookmark(url, title, category_name)
            return False
    ok = _write_bookmark_now(url, title, category_name)
    if not ok:
        # Direct write failed; fall back to queueing for the extension.
        with bookmarks_lock:
            _queue_bookmark(url, title, category_name)
        return False
    return True


# ── ZOTERO ──────────────────────────────────────────────────────────────────

def ZOTERO_COLLECTION_CACHE_FILE(): return os.path.join(OUTPUT_DIR(), "panop_zotero_collections.json")
def ZOTERO_URL_CACHE_FILE(): return os.path.join(OUTPUT_DIR(), "panop_zotero_urls.json")

_zotero_url_cache_lock = threading.Lock()
# by_url / by_doi → {"key": str, "version": int} so we can PATCH existing items.
_zotero_url_cache = {"urls": set(), "dois": set(), "by_url": {}, "by_doi": {},
                     "titlekeys": set(), "by_titlekey": {}, "ts": 0}

def _load_zotero_url_cache():
    data = load_json(ZOTERO_URL_CACHE_FILE(), {"urls": [], "dois": [], "by_url": {}, "by_doi": {}, "ts": 0})
    _zotero_url_cache["urls"] = set(data.get("urls", []))
    _zotero_url_cache["dois"] = set((d or "").lower() for d in data.get("dois", []) if d)
    _zotero_url_cache["by_url"] = dict(data.get("by_url", {}))
    _zotero_url_cache["by_doi"] = dict(data.get("by_doi", {}))
    _zotero_url_cache["titlekeys"] = set(data.get("titlekeys", []))
    _zotero_url_cache["by_titlekey"] = dict(data.get("by_titlekey", {}))
    _zotero_url_cache["ts"] = data.get("ts", 0)

def _save_zotero_url_cache():
    save_json(ZOTERO_URL_CACHE_FILE(), {
        "urls": sorted(_zotero_url_cache["urls"]),
        "dois": sorted(_zotero_url_cache["dois"]),
        "by_url": _zotero_url_cache["by_url"],
        "by_doi": _zotero_url_cache["by_doi"],
        "titlekeys": sorted(_zotero_url_cache["titlekeys"]),
        "by_titlekey": _zotero_url_cache["by_titlekey"],
        "ts": int(time.time()),
    })

def refresh_zotero_url_cache():
    """Walks the Panop collection tree in Zotero and caches every (url, doi)
    already present — used by send_to_zotero to short-circuit duplicate POSTs.
    Safe to call any time; no-op if creds missing.
    """
    env = get_env()
    if not env.get("zotero_api_key", "").strip() or not env.get("zotero_user_id", "").strip():
        return 0
    with _zotero_url_cache_lock:
        try:
            parent_name = env.get("bookmark_folder", "Panop") or "Panop"
            root_key = env.get("zotero_collection_key", "").strip() or _get_or_create_collection(parent_name)
            if not root_key: return 0
            # Discover child collections (Articles, Books, any custom)
            r = requests.get(
                f"{_zotero_base()}/collections/{root_key}/collections?limit=100",
                headers=_zotero_headers(), timeout=20
            )
            col_keys = [root_key]
            if r.status_code == 200:
                col_keys += [c["key"] for c in r.json()]
            urls, dois = set(), set()
            by_url, by_doi = {}, {}
            titlekeys, by_titlekey = set(), {}
            for ck in col_keys:
                start = 0
                while True:
                    rr = requests.get(
                        f"{_zotero_base()}/collections/{ck}/items?limit=100&start={start}",
                        headers=_zotero_headers(), timeout=30
                    )
                    if rr.status_code != 200: break
                    items = rr.json()
                    if not items: break
                    for it in items:
                        d = it.get("data") or {}
                        key, ver = d.get("key"), d.get("version")
                        u = d.get("url")
                        if u and key:
                            cu = canonicalize_url(u)
                            urls.add(u); urls.add(cu)
                            by_url[u] = {"key": key, "version": ver}
                            by_url[cu] = {"key": key, "version": ver}
                        raw_doi = d.get("DOI") or d.get("extra") or ""
                        m = DOI_RE.search(raw_doi) if raw_doi else None
                        if m and key:
                            dl = m.group(0).lower()
                            dois.add(dl)
                            by_doi[dl] = {"key": key, "version": ver}
                        tk = _title_dedup_key(d.get("title"), u or "")
                        if tk and key:
                            titlekeys.add(tk)
                            by_titlekey.setdefault(tk, {"key": key, "version": ver})
                    if len(items) < 100: break
                    start += len(items)
            _zotero_url_cache["urls"] = urls
            _zotero_url_cache["dois"] = dois
            _zotero_url_cache["by_url"] = by_url
            _zotero_url_cache["by_doi"] = by_doi
            _zotero_url_cache["titlekeys"] = titlekeys
            _zotero_url_cache["by_titlekey"] = by_titlekey
            _zotero_url_cache["ts"] = int(time.time())
            _save_zotero_url_cache()
            return len(urls)
        except Exception:
            return 0

def _zotero_headers():
    env = get_env()
    return {
        "Zotero-API-Key": env.get("zotero_api_key", "").strip(),
        "Content-Type": "application/json",
        "Zotero-API-Version": "3",
    }

def _zotero_base():
    env = get_env()
    uid = env.get("zotero_user_id", "").strip()
    return f"https://api.zotero.org/users/{uid}"

_zotero_lock = threading.Lock()

def _get_or_create_collection(name, parent_key=None):
    """Find a Zotero collection by (name, parent); create it if missing.
    Caches resolved keys on disk so we don't pound the API.
    """
    env = get_env()
    if not env.get("zotero_api_key", "").strip() or not env.get("zotero_user_id", "").strip():
        return None
    with _zotero_lock:
        cache = load_json(ZOTERO_COLLECTION_CACHE_FILE(), {})
        ck = f"{parent_key or 'root'}::{name}"
        if cache.get(ck):
            return cache[ck]
        try:
            # Search for existing collection under this parent
            r = requests.get(
                f"{_zotero_base()}/collections?limit=100",
                headers=_zotero_headers(), timeout=15
            )
            if r.status_code == 200:
                for c in r.json():
                    d = c.get("data", {})
                    got_parent = d.get("parentCollection") or None
                    want_parent = parent_key or None
                    if d.get("name") == name and got_parent == want_parent:
                        cache[ck] = c["key"]
                        save_json(ZOTERO_COLLECTION_CACHE_FILE(), cache)
                        return c["key"]
            # Create it
            body = [{"name": name, "parentCollection": parent_key if parent_key else False}]
            r = requests.post(
                f"{_zotero_base()}/collections",
                headers=_zotero_headers(), json=body, timeout=15
            )
            if r.status_code in (200, 201):
                data = r.json()
                successes = data.get("successful") or {}
                if successes:
                    key = list(successes.values())[0].get("key") or list(successes.values())[0].get("data", {}).get("key")
                    if key:
                        cache[ck] = key
                        save_json(ZOTERO_COLLECTION_CACHE_FILE(), cache)
                        return key
        except Exception:
            pass
    return None

_GENERIC_TITLES = {"", "untitled", "untitled pdf", "loading...", "redirecting...", "error"}

def _patch_zotero_item_if_richer(item_key, incoming_title=None, incoming_abstract=None,
                                 incoming_doi=None, incoming_tag=None):
    """Fetch the existing Zotero item; PATCH only fields where the incoming
    value is strictly better. Noop if nothing to improve. Respects Zotero's
    If-Unmodified-Since-Version header to avoid overwriting concurrent edits.
    """
    try:
        r = requests.get(f"{_zotero_base()}/items/{item_key}",
                         headers=_zotero_headers(), timeout=15)
        if r.status_code != 200: return False
        existing = r.json()
        d = existing.get("data") or {}
        version = d.get("version")
        patch = {}

        cur_title = (d.get("title") or "").strip()
        new_title = (incoming_title or "").strip()
        if new_title and new_title.lower() not in _GENERIC_TITLES:
            if cur_title.lower() in _GENERIC_TITLES or len(new_title) > len(cur_title) + 3:
                patch["title"] = new_title

        cur_abs = d.get("abstractNote") or ""
        new_abs = incoming_abstract or ""
        if new_abs and len(new_abs) > len(cur_abs):
            patch["abstractNote"] = new_abs

        if incoming_doi:
            doi_norm = incoming_doi.strip()
            cur_extra = d.get("extra") or ""
            if doi_norm and doi_norm.lower() not in cur_extra.lower():
                extra_lines = [l for l in cur_extra.splitlines() if not l.lower().startswith("doi:")]
                extra_lines.insert(0, f"DOI: {doi_norm}")
                patch["extra"] = "\n".join(extra_lines)

        if incoming_tag:
            cur_tags = [t.get("tag") for t in (d.get("tags") or [])]
            if incoming_tag not in cur_tags:
                patch["tags"] = [{"tag": t} for t in cur_tags + [incoming_tag]]

        if not patch: return False
        hdr = dict(_zotero_headers())
        if version is not None: hdr["If-Unmodified-Since-Version"] = str(version)
        pr = requests.patch(f"{_zotero_base()}/items/{item_key}",
                            headers=hdr, json=patch, timeout=15)
        return pr.status_code in (200, 204)
    except Exception:
        return False

def send_to_zotero(url, title, abstract, category_name, doi=None):
    """Posts a new item to the Zotero Web API. Auto-creates a parent folder
    (bookmark_folder, default 'Panop') and a per-category sub-collection,
    so Zotero mirrors the bookmark tree. Verifies the response body — Zotero
    returns 200 even when items fail, so we check `successful` / `failed`.

    DEDUP: checks the cached set of URLs/DOIs already in the Panop tree
    before POSTing. If already present, returns True without calling the API.
    """
    # Bookmark-only categories (data_tools / references / shopping / opportunities
    # / curios / study_work / content_longform) skip Zotero entirely — they save
    # to their bookmark folder only. Bruno 2026-06-15. Articles/Books/Science News
    # still go to Zotero.
    try:
        for _c in (load_config() or {}).get("categories", []):
            if _c.get("name") == category_name and _c.get("route") == "bookmark":
                return False
    except Exception:
        pass
    env = get_env()
    api_key = env.get("zotero_api_key", "").strip()
    user_id = env.get("zotero_user_id", "").strip()
    if not api_key or not user_id:
        return False
    # QUALITY GATE — never save a block page / error / contentless extraction.
    junk = _is_junk_page(title, url, abstract)
    if junk:
        try:
            _record_accountability_event("save_blocked_junk", url,
                                         {"title": title}, target="zotero", reason=junk)
        except Exception:
            pass
        return False
    try:
        # Dedup short-circuit — if we already have this item in Zotero,
        # try to upgrade it with any fields we have that it doesn't.
        with _zotero_url_cache_lock:
            url_set = set(_zotero_url_cache["urls"])
            doi_set = set(_zotero_url_cache["dois"])
            by_url = dict(_zotero_url_cache["by_url"])
            by_doi = dict(_zotero_url_cache["by_doi"])
            titlekey_set = set(_zotero_url_cache["titlekeys"])
            by_titlekey = dict(_zotero_url_cache["by_titlekey"])
        canon = canonicalize_url(url)
        tkey = _title_dedup_key(title, canon or url)
        hit = None
        if url in url_set:         hit = by_url.get(url)
        elif canon in url_set:     hit = by_url.get(canon)
        elif doi and doi.lower() in doi_set: hit = by_doi.get(doi.lower())
        # Same article under a different URL (pre-redirect vs resolved, tracking
        # variants) — catch it by normalized title+host. THIS is what stops the
        # duplicate explosion the URL-only dedup let through. Bruno 2026-06-14.
        elif tkey and tkey in titlekey_set: hit = by_titlekey.get(tkey)
        if hit and hit.get("key"):
            try:
                _patch_zotero_item_if_richer(
                    hit["key"],
                    incoming_title=title, incoming_abstract=abstract,
                    incoming_doi=doi, incoming_tag=category_name
                )
            except Exception:
                pass
            return True

        parent_folder_name = env.get("bookmark_folder", "Panop") or "Panop"
        root_key = env.get("zotero_collection_key", "").strip() or _get_or_create_collection(parent_folder_name)
        cat_key = _get_or_create_collection(category_name, parent_key=root_key) if root_key else None
        cols = [k for k in [cat_key or root_key] if k]
        item = {
            "itemType": "webpage",
            "title": title or "Untitled",
            "url": canon or url,
            "abstractNote": abstract or "",
            "date": datetime.now().strftime("%Y-%m-%d"),
            "tags": [{"tag": category_name}] if category_name else [],
            "collections": cols,
        }
        if doi:
            item["extra"] = f"DOI: {doi}"
        resp = requests.post(
            f"{_zotero_base()}/items",
            headers=_zotero_headers(), json=[item], timeout=20
        )
        if resp.status_code not in (200, 201):
            return False
        body = resp.json() if resp.content else {}
        ok = bool(body.get("successful")) and not body.get("failed")
        if ok:
            # body.successful is {"0": {"key":..., "version":..., "data": {...}}}
            created = next(iter((body.get("successful") or {}).values()), None)
            new_key = (created or {}).get("key") or ((created or {}).get("data") or {}).get("key")
            new_ver = (created or {}).get("version") or ((created or {}).get("data") or {}).get("version")
            ref = {"key": new_key, "version": new_ver} if new_key else None
            with _zotero_url_cache_lock:
                _zotero_url_cache["urls"].add(canon or url)
                _zotero_url_cache["urls"].add(url)
                if ref:
                    _zotero_url_cache["by_url"][canon or url] = ref
                    _zotero_url_cache["by_url"][url] = ref
                if doi:
                    dl = doi.lower()
                    _zotero_url_cache["dois"].add(dl)
                    if ref: _zotero_url_cache["by_doi"][dl] = ref
                if tkey:
                    _zotero_url_cache["titlekeys"].add(tkey)
                    if ref: _zotero_url_cache["by_titlekey"].setdefault(tkey, ref)
        return ok
    except Exception:
        return False

def _adb_list_devices(adb_exe):
    """Returns (ready_devices, unauthorized_devices, offline_devices) lists of ids.

    Bruno 2026-05-27 — DO NOT run `adb start-server` on every call. When the
    daemon is down, adb forks its server process, and adb's *internal* daemon
    spawn shows a console window that our Popen CREATE_NO_WINDOW patch CANNOT
    suppress (it's adb.exe doing the spawning, not us). This function is polled
    every 6 s by the watchdog loop, so the unconditional start-server was
    flashing a shell window ~every second (worse with duplicate Panop
    instances). Fix: call `adb devices` directly — it connects to the existing
    daemon silently; only if that fails do we start the server ONCE and retry.
    The startup hook starts the daemon once at boot (hidden), so steady state
    never re-forks it.
    """
    def _devices():
        return subprocess.run([adb_exe, "devices"], capture_output=True,
                              text=True, timeout=15)
    try:
        r = _devices()
    except Exception:
        try:
            subprocess.run([adb_exe, "start-server"], capture_output=True, timeout=15)
            r = _devices()
        except Exception:
            return [], [], []
    ready, unauth, offline = [], [], []
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if not line or line.startswith("List of") or line.startswith("*"):
            continue
        parts = line.split("\t")
        if len(parts) < 2: continue
        did, state = parts[0].strip(), parts[1].strip()
        if state == "device":        ready.append(did)
        elif state == "unauthorized": unauth.append(did)
        elif state == "offline":      offline.append(did)
    return ready, unauth, offline

def run_adb_sweep():
    global sweep_status
    sweep_status["running"] = True
    sweep_status["last_run"] = datetime.now().isoformat()
    sweep_status["last_error"] = None
    sweep_status["tabs_seen"] = 0
    sweep_status["tabs_new"] = 0
    sweep_status["tabs_matched"] = 0
    sweep_status["chrome_running"] = is_chrome_running()
    try:
        adb_exe = ensure_adb()
        config = load_config()
        env = get_env()
        init_dirs()

        ready, unauth, offline = _adb_list_devices(adb_exe)

        if not ready:
            # Try wireless reconnect (accept bare IP by defaulting to :5555)
            for ip in config.get("wireless_ips", []):
                ip = (ip or "").strip()
                if not ip: continue
                target = ip if ":" in ip else f"{ip}:5555"
                try:
                    subprocess.run([adb_exe, "connect", target], capture_output=True, timeout=15)
                except Exception:
                    pass
            ready, unauth, offline = _adb_list_devices(adb_exe)

        sweep_status["adb_connected"] = bool(ready)
        sweep_status["device_id"] = ready[0] if ready else None

        if not ready:
            if unauth:
                sweep_status["last_error"] = (
                    f"Device {unauth[0]} is UNAUTHORIZED. On your phone, accept the "
                    f"'Allow USB debugging' prompt (tick 'Always allow from this computer'), "
                    f"then click FETCH NOW again."
                )
            elif offline:
                sweep_status["last_error"] = (
                    f"Device {offline[0]} is OFFLINE. Unplug + replug USB, or toggle "
                    f"Wireless Debugging off/on, then retry."
                )
            else:
                sweep_status["last_error"] = (
                    "No Android device found. Check: (1) USB Debugging ON in Developer Options, "
                    "(2) cable supports data (not just charging), (3) accept the RSA prompt on the phone, "
                    "or add the phone's Wireless Debugging IP (e.g. 192.168.1.42:5555) in System Settings."
                )
            sweep_status["running"] = False
            return
            
        # Re-establish the ADB→DevTools forward, with a few retries. The socket
        # sometimes drops mid-session (phone sleeps, Chrome backgrounds), so we
        # kill + recreate the forward between retries instead of just retrying
        # the HTTP call against a stale tunnel.
        tabs = None
        last_err = None
        for attempt in range(4):
            try:
                subprocess.run([adb_exe, "forward", "--remove", "tcp:9222"], capture_output=True)
            except Exception:
                pass
            subprocess.run([adb_exe, "forward", "tcp:9222", "localabstract:chrome_devtools_remote"], capture_output=True)
            try:
                resp = requests.get("http://127.0.0.1:9222/json/list", timeout=60)
                if resp.status_code == 200:
                    tabs = resp.json()
                    break
                last_err = f"HTTP {resp.status_code}"
            except Exception as e:
                last_err = str(e)
            time.sleep(1.5)
        # Wake any tabs Chrome Android suspended to RAM (URL == "" / about:blank
        # but DevTools target still exists). Activating the target forces Chrome
        # to restore the page so its real URL becomes visible. We do up to 2
        # passes — each /json/activate is asynchronous on the phone.
        if tabs is not None and env.get("wake_suspended_tabs", True):
            for wake_pass in range(2):
                suspended = [t for t in tabs if not (t.get("url") or "").strip() or t.get("url") == "about:blank"]
                if not suspended:
                    break
                for t in suspended:
                    tid = t.get("id")
                    if not tid: continue
                    try:
                        requests.post(f"http://127.0.0.1:9222/json/activate/{tid}", timeout=3)
                    except Exception:
                        pass
                time.sleep(2.5)  # give Chrome a moment to restore
                try:
                    r2 = requests.get("http://127.0.0.1:9222/json/list", timeout=60)
                    if r2.status_code == 200:
                        tabs = r2.json()
                except Exception:
                    pass
            sweep_status["tabs_seen"] = len(tabs)
        if tabs is not None:
            sweep_status["last_tab_urls"] = [t.get("url","") for t in tabs if t.get("url")]
            sweep_status["last_tab_fetch_at"] = datetime.now().isoformat()
        if tabs is None:
            sweep_status["last_error"] = (
                f"Android DevTools unreachable ({last_err}). On your phone: open Chrome, "
                "bring it to the foreground (or at least recent apps), and make sure USB/Wireless "
                "Debugging is still authorized. Then retry."
            )
            sweep_status["running"] = False
            return
        sweep_status["tabs_seen"] = len(tabs)
        history = load_history()
        categories = config.get("categories", [])
        strict = env.get("strict_domain_scan", True)
        catch_uncat = env.get("catch_uncategorized", False)

        # ── PHASE 1: Pure string matching (no network, instant) ──────────────
        # Build list of (tab, cat, needs_body_fetch) for candidates only
        candidates = []  # (tab, matched_cat_no_body_check, needs_fetch)
        for tab in tabs:
            url = tab.get("url", "")
            true_url = _resolve_terminal_tab_url(url, env)
            storage_probe = canonicalize_url(true_url) or true_url
            if not url or url.startswith("chrome://") or url in history or storage_probe in history:
                continue
            sweep_status["tabs_new"] += 1
            url_lower = true_url.lower()
            is_pdf = url_lower.endswith(".pdf")

            # Check never_academic list to skip unnecessary fetches
            try:
                import lib.classifier as classifier
                res = classifier.classify(true_url, None)
                if res.layer == "domain_tier" and res.action == "abstain":
                    reason = (res.evidence or {}).get("reason", "")
                    if isinstance(reason, str) and reason.startswith("never_academic:"):
                        continue
            except Exception:
                pass

            domain_matched_cat = None
            needs_fetch = False

            # Check if smart classifier matches directly without page content
            try:
                import lib.classifier as classifier
                res = classifier.classify(true_url, None)
                if res.action == "match" and res.category:
                    matched = next((c for c in categories if c.get("id") == res.category), None)
                    if matched:
                        domain_matched_cat = matched
                        needs_fetch = True
            except Exception:
                pass

            if not domain_matched_cat:
                for cat in categories:
                    domains = cat.get("domain_keywords", [])
                    body_req = cat.get("body_required", [])
                    body_forb = cat.get("body_forbidden", [])
                    tab_group = cat.get("tab_group", "")

                    if tab_group and tab_group.lower() not in str(tab).lower():
                        continue

                    domain_match = any(d.lower() in url_lower for d in domains if d) if domains else True

                    if not domain_match and strict and domains:
                        continue  # strict mode: skip if no domain match

                    if domain_match or not domains:
                        domain_matched_cat = cat
                        needs_fetch = True
                        break

            if domain_matched_cat:
                candidates.append((tab, domain_matched_cat, needs_fetch))
            elif catch_uncat and not any(url.startswith("chrome://") for _ in [1]):
                candidates.append((tab, {"name": "Uncategorized", "id": "uncategorized",
                                         "dest_folder": os.path.join(OUTPUT_DIR(), "Uncategorized")}, True))

        sweep_status["tabs_seen"] = len(tabs)

        # ── PHASE 2: Parallel page fetches for candidates ────────────────────
        def process_tab(tab, cat, needs_fetch):
            """Worker: fetch page if needed, run smart classification, return result or None."""
            url = tab.get("url", "")
            terminal_url = _resolve_terminal_tab_url(url, env)
            url_lower = terminal_url.lower()
            is_pdf = url_lower.endswith(".pdf")

            metadata = None
            if needs_fetch and not is_pdf:
                metadata = fetch_page_content(terminal_url)  # returns None on failure

            matched_category = _classify_tab_candidate(terminal_url, metadata, categories, env)
            if not matched_category:
                if catch_uncat:
                    matched_category = next((c for c in categories if c.get("id") == "uncategorized"), None)
                    if not matched_category:
                        matched_category = {"name": "Uncategorized", "id": "uncategorized", "dest_folder": os.path.join(OUTPUT_DIR(), "Uncategorized")}
                else:
                    return None

            # Title: prefer page metadata, fall back to PDF-aware resolution, then DevTools title
            if is_pdf:
                title = get_pdf_title(terminal_url, tab.get("title", ""))
            else:
                title = (metadata or {}).get("title") or tab.get("title", "") or "Untitled"
            return (terminal_url, matched_category, title, metadata or {}, tab.get("id"))

        # Run up to 8 tabs in parallel — enough throughput without hammering RAM/CPU
        WORKERS = 8
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futures = {pool.submit(process_tab, tab, cat, needs_fetch): (tab, cat)
                       for tab, cat, needs_fetch in candidates}

            for future in as_completed(futures):
                try:
                    result = future.result()
                    if result is None:
                        continue
                    url, matched_category, title, metadata, tab_id = result

                    # Prefer canonical URL as the storage key (collapses m./www./tracking variants)
                    canon_url = canonicalize_url(url)
                    storage_url = canon_url or url
                    doi = metadata.get("doi") if metadata else None

                    # Skip if another parallel worker already saved this url
                    h = load_history()
                    if storage_url in h or url in h:
                        continue

                    # PROACTIVE DEDUPLICATION: DOI > canonical URL > normalized title
                    existing_url = None
                    if doi:
                        for u, item in h.items():
                            if (item.get("doi") or "").lower() == doi.lower():
                                existing_url = u; break
                    if not existing_url:
                        for u in h.keys():
                            if canonicalize_url(u) == canon_url:
                                existing_url = u; break
                    if not existing_url:
                        norm = normalize_title(title)
                        if norm and norm not in {"untitled", "untitled pdf"}:
                            for u, item in h.items():
                                if normalize_title(item.get("title")) == norm:
                                    existing_url = u; break

                    if existing_url:
                        # Merge this "new" found tab into the existing history record
                        h[existing_url] = merge_entries(h[existing_url], {
                            "url": url, "title": title, "category": matched_category["name"],
                            "cat_id": matched_category["id"], "abstract": metadata.get("abstract", ""),
                            "doi": doi, "canonical_url": metadata.get("canonical_url", url),
                            "date": datetime.now().isoformat()
                        })
                        save_history(h)
                        sweep_status["tabs_matched"] += 1
                        continue

                    safe_t = "".join([c for c in title if c.isalpha() or c.isdigit() or c == ' ']).rstrip()
                    if not safe_t: safe_t = str(int(datetime.now().timestamp()))

                    if metadata.get("text") and matched_category["id"] != "uncategorized":
                        update_ai_profile(matched_category["id"], metadata["text"])

                    d = matched_category.get("dest_folder", matched_category["name"])
                    target_dir = d if os.path.isabs(d) else os.path.join(OUTPUT_DIR(), d)
                    os.makedirs(target_dir, exist_ok=True)

                    # Save rich .json entry (replaces old .md)
                    entry_data = {
                        "url": url,
                        "canonical_url": metadata.get("canonical_url", url),
                        "title": title,
                        "category": matched_category["name"],
                        "category_id": matched_category["id"],
                        "abstract": metadata.get("abstract", ""),
                        "date_saved": datetime.now().isoformat(),
                        "source": "panop-android"
                    }
                    fname = safe_t.replace(' ', '_')[:80]  # cap filename length
                    with open(os.path.join(target_dir, f"{fname}.json"), "w", encoding="utf-8") as f:
                        json.dump(entry_data, f, indent=2, ensure_ascii=False)

                    z_ok = send_to_zotero(storage_url, title, metadata.get("abstract", ""), matched_category["name"], doi=doi)
                    b_ok = add_chrome_bookmark(storage_url, title, matched_category["name"])

                    # IMPORTANT: write to `h` (just-loaded, line 1131), NOT the
                    # outer `history` which was loaded once at sweep start and
                    # is now stale. Writing stale `history` back to disk would
                    # clobber all changes made by prior iterations (e.g. merges
                    # done through `h`). This was causing the "matched goes up
                    # but total stays the same" symptom.
                    h[storage_url] = {
                        "title": title,
                        "category": matched_category["name"],
                        "cat_id": matched_category["id"],
                        "date": datetime.now().isoformat(),
                        "abstract": metadata.get("abstract", ""),
                        "canonical_url": metadata.get("canonical_url", storage_url),
                        "original_url": url if url != storage_url else None,
                        "doi": doi,
                        "ai_learned": False,
                        "file": os.path.join(target_dir, f"{fname}.json"),
                        "z_synced": z_ok,
                        "b_synced": b_ok
                    }
                    _stamp_accountability(
                        h[storage_url], storage_url, "history_upsert", "adb_sweep",
                        classification={
                            "cat_id": matched_category.get("id"),
                            "category": matched_category.get("name"),
                            "source": matched_category.get("_classification_source") or "configured_rules",
                            "confidence": matched_category.get("_classification_confidence"),
                            "reason": matched_category.get("_classification_reason"),
                        },
                    )
                    _record_accountability_event("history_upsert", storage_url, h[storage_url], source="adb_sweep")
                    # Science-News 2nd stage: a digest/roundup explodes into its
                    # contained articles/books, each classified + saved to its own
                    # destination (writes into `h`; persisted by save_history below).
                    # No-ops for a single news story (no primary links). Best-effort.
                    if matched_category.get("id") == "science_news":
                        try:
                            ss = second_stage_extract(storage_url, env, categories, history=h)
                            if ss.get("saved"):
                                h[storage_url]["second_stage"] = {"found": ss["found"],
                                    "saved": ss["saved"], "by_cat": ss["by_cat"]}
                        except Exception:
                            pass
                    save_history(h)
                    sweep_status["tabs_matched"] += 1

                    # AUTO-CLEANUP: Close tab on phone ONLY if enabled AND fully synced
                    # AND it has a real category match (hard safety gate — see _safe_to_close).
                    if env.get("close_tabs_after_save") and not _manual_vetting_required(env) and tab_id and _safe_to_close(h[storage_url]):
                        _close_devtools_tab(tab_id, storage_url, h[storage_url], "sweep_after_save")

                except Exception:
                    continue

        # Post-sweep autonomous dedup cleanup
        try:
            consolidate_history()
        except Exception:
            pass

        # If close_tabs_after_save is on, schedule a delayed pass that closes
        # tabs whose bookmarks were QUEUED during this sweep (Chrome is running,
        # so add_chrome_bookmark returned False → b_ok=False → tab stayed open).
        # The extension drains the queue within ~30s and ACKs back, flipping
        # b_synced=true; a 75 s later pass finds them properly synced and closes
        # them via the DevTools endpoint.
        if env.get("close_tabs_after_save") and not _manual_vetting_required(env):
            def _delayed_close():
                time.sleep(75)
                try: _do_close_synced_tabs_now()
                except Exception: pass
            threading.Thread(target=_delayed_close, daemon=True).start()
    except Exception as e:
        sweep_status["last_error"] = str(e)
    finally:
        try:
            pending = load_json(PENDING_BOOKMARKS_FILE(), [])
            sweep_status["bookmarks_pending"] = len(pending)
            sweep_status["chrome_running"] = is_chrome_running()
        except Exception:
            pass
        sweep_status["running"] = False

def adb_loop():
    """Background timer loop. Waits for the configured interval FIRST,
    then sweeps. This means startup is instant — sweeps only happen on schedule
    or when the user manually clicks FETCH NOW.
    """
    while True:
        env = get_env()
        if not env.get("enable_autonomous_sweep", False):
            time.sleep(60)
            continue
        hours = env.get("interval_hours", 6)
        if hours < 0.1: hours = 0.1
        time.sleep(hours * 3600)  # wait first, then sweep
        if get_env().get("enable_autonomous_sweep", False):
            run_adb_sweep()

@app.on_event("startup")
def start_background_jobs():
    import gc
    init_dirs()
    # Start the ADB daemon exactly ONCE, hidden, at boot. Our subprocess patch
    # adds CREATE_NO_WINDOW so this single start-server doesn't flash; once the
    # daemon is up it persists, so the per-poll `adb devices` calls below
    # connect silently and never re-fork it. This is the other half of the
    # "no shell window every second" fix. Bruno 2026-05-27.
    try:
        _adb_exe0 = ensure_adb()
        subprocess.run([_adb_exe0, "start-server"], capture_output=True, timeout=15)
    except Exception:
        pass
    # Keep the wireless ADB link alive in the background. Android rotates
    # the connect port whenever Wireless Debugging idles or toggles, which
    # causes manual reconnects to feel flaky. The watchdog auto-discovers
    # the new port via mDNS and reconnects within ~6 s.
    try: _start_adb_watchdog()
    except Exception: pass
    # Kill any stale panop-server siblings left from a previous crashed run.
    # Bruno 2026-05-27 (revised): match the exact ABSOLUTE PATH of this
    # main.py against each candidate process's positional arguments — not
    # against the cmdline as a whole string. Otherwise we false-positive on
    # test harnesses / scripts whose heredoc text happens to mention
    # "panop_server" or "main.py" and end up TERMing them (that bit our own
    # comparison/perf scripts). Exactly one Panop ever owns port 8000.
    try:
        import psutil
        me = os.getpid()
        my_main_norm = os.path.normcase(os.path.abspath(__file__))
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                if proc.info['pid'] == me:
                    continue
                cmdline = proc.info.get('cmdline') or []
                # Skip argv[0] (the python interpreter path). For every other
                # arg, only treat it as a "sibling" if it's literally this
                # same main.py file.
                for arg in cmdline[1:]:
                    try:
                        if os.path.normcase(os.path.abspath(str(arg))) == my_main_norm:
                            proc.kill()
                            break
                    except Exception:
                        continue
            except Exception:
                continue
    except Exception:
        pass
    # Load cached Zotero URL set from last run so dedup works immediately,
    # then refresh from API + scan bookmarks in the background.
    try:
        _load_zotero_url_cache()
    except Exception:
        pass
    def _startup_reconcile():
        try: reconcile_all()
        except Exception: pass
    threading.Thread(target=_startup_reconcile, daemon=True).start()
    threading.Thread(target=adb_loop, daemon=True).start()

@app.get("/api/v1/config")
def get_co(): return load_config()

@app.post("/api/v1/config")
def update_co(req_data: dict):
    # Merge: never let a save wipe out wireless_ips unless the user explicitly cleared them
    existing = load_config()
    # If incoming request has empty wireless_ips but stored copy has entries, preserve stored ones
    if not req_data.get("wireless_ips") and existing.get("wireless_ips"):
        req_data["wireless_ips"] = existing["wireless_ips"]
    save_json(CONFIG_FILE(), req_data)
    init_dirs()
    return {"status": "updated"}

@app.get("/api/v1/env")
def read_env(): return get_env()

@app.post("/api/v1/env")
def update_ev(data: dict):
    env = get_env()
    if isinstance(data, dict):
        env.update(data)
    save_env(env)
    init_dirs()
    return {"status": "ok", "env": env}

@app.get("/api/v1/status")
def get_status():
    try:
        adb_exe = ensure_adb()
        ready, _u, _o = _adb_list_devices(adb_exe)
        sweep_status["adb_connected"] = bool(ready)
        sweep_status["device_id"] = ready[0] if ready else None
    except Exception:
        pass
    return sweep_status

@app.get("/api/v1/history")
def get_hi(): return load_history()

@app.get("/api/v1/history/meta")
def get_hi_meta():
    """Lightweight endpoint: returns count + a version token.
    Version includes sync flags so toggling z_synced/b_synced also bumps it,
    which lets the UI detect bulk-sync progress and refresh the ledger."""
    h = load_history()
    sig = tuple(
        (u, bool(v.get("z_synced")), bool(v.get("b_synced")))
        for u, v in sorted(h.items())
    )
    z = sum(1 for v in h.values() if v.get("z_synced"))
    b = sum(1 for v in h.values() if v.get("b_synced"))
    return {"count": len(h), "version": hash(sig) & 0xFFFFFFFF, "z_synced": z, "b_synced": b}


class EditItem(BaseModel):
    old_url: str
    url: str
    title: str
    category_id: str
    date: str

def _auto_sync_entry(url):
    """Fire-and-forget Zotero + bookmark sync for a single history entry.
    Skips whichever side is already synced. Safe on missing creds (no-op)."""
    try:
        h = load_history()
        item = h.get(url)
        if not item: return
        if not item.get("z_synced"):
            if send_to_zotero(url, item.get("title"), item.get("abstract"),
                              item.get("category"), doi=item.get("doi")):
                item["z_synced"] = True
        if not item.get("b_synced"):
            # Queues if Chrome is running; extension drains it within ~30s.
            if add_chrome_bookmark(url, item.get("title"), item.get("category")):
                item["b_synced"] = True
        _stamp_accountability(item, url, "sync_update", "auto_sync")
        _record_accountability_event("sync_update", url, item, source="auto_sync")
        save_history(h)
    except Exception:
        pass

@app.post("/api/v1/history/edit")
def edit_hi(item: EditItem, background_tasks: BackgroundTasks):
    h = load_history()
    if item.old_url in h:
        val = h[item.old_url]
        val["title"] = item.title
        val["date"] = item.date
        config = load_config()
        cat = next((c for c in config["categories"] if c["id"] == item.category_id), None)
        if cat: val.update({"cat_id": cat["id"], "category": cat["name"]})

        if item.url != item.old_url:
            del h[item.old_url]
            # If URL changed, the new URL is effectively a new entry — reset sync flags
            # so the auto-sync below actually pushes it to Zotero/Bookmarks.
            val["z_synced"] = False
            val["b_synced"] = False
            h[item.url] = val
        save_history(h)
        background_tasks.add_task(_auto_sync_entry, item.url)
    return {"status": "ok"}

class DeleteItem(BaseModel): urls: List[str]

@app.post("/api/v1/history/delete")
def del_hi(item: DeleteItem):
    h = load_history()
    for u in item.urls:
        if u in h:
            # Also delete the associated file from disk if it exists
            file_path = h[u].get("file", "")
            if file_path and os.path.exists(file_path):
                try: os.remove(file_path)
                except Exception: pass
            del h[u]
    save_history(h)
    return {"status": "ok"}

def OVERRIDES_FILE():
    _root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(_root, "state", "panop", "panop_classifier_overrides.json")

def CORRECTION_LOG_FILE():
    _root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(_root, "state", "panop", "panop_classifier_corrections.jsonl")

def LEARNED_RULES_FILE():
    _root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(_root, "state", "panop", "panop_classifier_learned_rules.json")

def load_overrides():
    path = OVERRIDES_FILE()
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        save_json(path, {})
        return {}
    return load_json(path, {})

def save_overrides(o):
    path = OVERRIDES_FILE()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    save_json(path, o)

_classifier_corrections_lock = threading.Lock()
_LEARNED_DOMAIN_MIN_CORRECTIONS = 3

def _host_for_url(url):
    try:
        from urllib.parse import urlparse
        host = (urlparse(canonicalize_url(url) or url).netloc or "").lower()
        if host.startswith("www."):
            host = host[4:]
        return host
    except Exception:
        return ""

def _category_by_id(categories, cat_id):
    cat_id = str(cat_id or "").strip()
    if cat_id.lower() == "uncategorized":
        return {"id": "uncategorized", "name": "Uncategorized", "dest_folder": "Uncategorized"}
    return next((c for c in categories if c.get("id") == cat_id), None)

def _unique_override_entries(overrides):
    seen = set()
    for key, item in (overrides or {}).items():
        if not isinstance(item, dict):
            continue
        url = item.get("url") or key
        canon = item.get("canonical_url") or canonicalize_url(url) or url
        if canon in seen:
            continue
        seen.add(canon)
        yield item

def _append_correction_events(events):
    if not events:
        return
    path = CORRECTION_LOG_FILE()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for event in events:
            f.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")

def _rebuild_learned_rules(overrides=None):
    overrides = overrides if overrides is not None else load_overrides()
    host_counts = {}
    host_examples = {}
    for item in _unique_override_entries(overrides):
        url = item.get("canonical_url") or item.get("url") or ""
        host = item.get("host") or _host_for_url(url)
        cat_id = str(item.get("category_id") or "uncategorized").strip() or "uncategorized"
        if not host:
            continue
        host_counts.setdefault(host, Counter())[cat_id] += 1
        host_examples.setdefault(host, {}).setdefault(cat_id, [])
        if len(host_examples[host][cat_id]) < 5:
            host_examples[host][cat_id].append({
                "url": url,
                "title": item.get("title", ""),
                "reason": item.get("reason", ""),
                "updated_at": item.get("updated_at", ""),
            })

    domains = {}
    for host, counts in host_counts.items():
        total = sum(counts.values())
        if total < _LEARNED_DOMAIN_MIN_CORRECTIONS or len(counts) != 1:
            continue
        cat_id, count = counts.most_common(1)[0]
        confidence = min(0.98, 0.70 + (0.05 * min(count, 5)))
        domains[host] = {
            "host": host,
            "category_id": cat_id,
            "count": count,
            "total_corrections": total,
            "confidence": round(confidence, 3),
            "source": "consistent_user_corrections",
            "enabled": True,
            "examples": host_examples.get(host, {}).get(cat_id, []),
            "updated_at": datetime.now().isoformat(),
            "reason": f"Learned from {count} consistent user correction(s) on {host}.",
        }

    learned = {
        "version": 1,
        "min_domain_corrections": _LEARNED_DOMAIN_MIN_CORRECTIONS,
        "updated_at": datetime.now().isoformat(),
        "domains": domains,
    }
    path = LEARNED_RULES_FILE()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    save_json(path, learned)
    return learned

def load_learned_rules():
    return load_json(LEARNED_RULES_FILE(), {
        "version": 1,
        "min_domain_corrections": _LEARNED_DOMAIN_MIN_CORRECTIONS,
        "domains": {},
    })

def _classifier_override_decision(url):
    storage_url = canonicalize_url(url) or url
    overrides = load_overrides()
    matched_override = overrides.get(url) or overrides.get(storage_url)
    if matched_override:
        cat_id = str(matched_override.get("category_id") or "uncategorized").strip() or "uncategorized"
        action = "block" if cat_id.lower() == "uncategorized" else "match"
        return {
            "action": action,
            "category_id": cat_id,
            "source": "exact_user_correction",
            "confidence": 1.0,
            "reason": matched_override.get("reason") or "User correction for this exact URL.",
            "details": matched_override,
        }

    host = _host_for_url(storage_url)
    rule = (load_learned_rules().get("domains") or {}).get(host)
    if rule and rule.get("enabled", True):
        cat_id = str(rule.get("category_id") or "uncategorized").strip() or "uncategorized"
        action = "block" if cat_id.lower() == "uncategorized" else "match"
        return {
            "action": action,
            "category_id": cat_id,
            "source": "learned_domain_rule",
            "confidence": float(rule.get("confidence") or 0.0),
            "reason": rule.get("reason") or f"Learned domain rule for {host}.",
            "details": rule,
        }
    return None

@app.post("/api/v1/classifier/overrides")
def add_classifier_overrides(payload: dict):
    items = payload.get("items") or []
    events = []
    with _classifier_corrections_lock:
        o = load_overrides()
        now = datetime.now().isoformat()
        changed = 0
        for item in items:
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            storage_url = canonicalize_url(url) or url
            cat_id = str(item.get("category_id") or "uncategorized").strip() or "uncategorized"
            entry = {
                "url": url,
                "canonical_url": storage_url,
                "host": _host_for_url(storage_url),
                "title": item.get("title", ""),
                "category_id": cat_id,
                "previous_category_id": item.get("previous_category_id", ""),
                "previous_category_name": item.get("previous_category_name", ""),
                "previous_status": item.get("previous_status", ""),
                "previous_reason": item.get("previous_reason", ""),
                "decision_quality": item.get("decision_quality", ""),
                "reason": item.get("reason", ""),
                "updated_at": now,
                "source": "inbox_user_correction",
            }
            o[url] = entry
            o[storage_url] = entry
            events.append({
                "event": "classifier_correction",
                "url": url,
                "canonical_url": storage_url,
                "host": entry["host"],
                "title": entry["title"],
                "category_id": cat_id,
                "previous_category_id": entry["previous_category_id"],
                "previous_category_name": entry["previous_category_name"],
                "previous_status": entry["previous_status"],
                "previous_reason": entry["previous_reason"],
                "decision_quality": entry["decision_quality"],
                "reason": entry["reason"],
                "ts": now,
            })
            changed += 1
        save_overrides(o)
        learned = _rebuild_learned_rules(o)
        _append_correction_events(events)
    return {
        "status": "ok",
        "count": changed,
        "exact_overrides": len(list(_unique_override_entries(o))),
        "learned_domains": len((learned.get("domains") or {})),
    }

@app.get("/api/v1/classifier/overrides")
def get_classifier_overrides():
    return load_overrides()

@app.get("/api/v1/classifier/corrections/stats")
def get_classifier_correction_stats():
    overrides = load_overrides()
    learned = load_learned_rules()
    unique = list(_unique_override_entries(overrides))
    by_category = Counter(str(item.get("category_id") or "uncategorized") for item in unique)
    return {
        "status": "ok",
        "exact_overrides": len(unique),
        "learned_domains": len((learned.get("domains") or {})),
        "by_category": dict(sorted(by_category.items())),
        "learned": learned,
    }

class AddLinkItem(BaseModel):
    url: str
    title: str = ""
    category_id: str = ""

@app.post("/api/v1/history/add")
def add_hi(item: AddLinkItem, background_tasks: BackgroundTasks):
    h = load_history()
    url = item.url.strip()
    storage_url = canonicalize_url(url) or url
    if storage_url in h:
        return {"status": "already_exists", "message": "URL already exists in history."}
    
    title = item.title.strip() or url
    
    # Resolve category
    config = load_config()
    categories = config.get("categories", [])
    matched_cat = None
    if item.category_id:
        if item.category_id == "uncategorized":
            matched_cat = {"id": "uncategorized", "name": "Uncategorized", "dest_folder": "Uncategorized"}
        else:
            matched_cat = next((c for c in categories if c["id"] == item.category_id), None)
    
    if not matched_cat:
        # Check user overrides fallback
        try:
            overrides = load_overrides()
            matched_override = overrides.get(url) or overrides.get(storage_url)
            if matched_override:
                cat_id = matched_override.get("category_id")
                if cat_id == "uncategorized":
                    matched_cat = {"id": "uncategorized", "name": "Uncategorized", "dest_folder": "Uncategorized"}
                else:
                    matched_cat = next((c for c in categories if c["id"] == cat_id), None)
        except Exception:
            pass
    
    if not matched_cat:
        # Auto-detect category
        url_lower = url.lower()
        for cat in categories:
            domains = cat.get("domain_keywords", [])
            if domains and any(d.lower() in url_lower for d in domains if d):
                matched_cat = cat
                break
                
    if not matched_cat:
        matched_cat = {"id": "uncategorized", "name": "Uncategorized", "dest_folder": "Uncategorized"}
        
    # Save history entry
    h[storage_url] = {
        "title": title,
        "category": matched_cat["name"],
        "cat_id": matched_cat["id"],
        "date": datetime.now().isoformat(),
        "abstract": "",
        "canonical_url": storage_url,
        "original_url": url if url != storage_url else None,
        "doi": "",
        "ai_learned": False,
        "file": "",
        "z_synced": False,
        "b_synced": False
    }
    save_history(h)
    
    # Auto-sync entry in background tasks (Zotero + Bookmarks)
    background_tasks.add_task(_auto_sync_entry, storage_url)
    
    return {"status": "ok", "category": matched_cat["name"]}

@app.post("/api/v1/fetch_now")
def f_now(background_tasks: BackgroundTasks):
    # Flip state synchronously so the UI's status poll doesn't race the worker
    # thread and see the PREVIOUS run's running=false before this one starts.
    sweep_status["running"] = True
    sweep_status["last_error"] = None
    sweep_status["tabs_seen"] = 0
    sweep_status["new_entries"] = 0
    sweep_status["matched"] = 0
    background_tasks.add_task(run_adb_sweep)
    return {"status": "fetching"}

enrich_status = {"running": False, "total": 0, "done": 0, "updated": 0, "last_run": None, "cancel": False}

def run_enrich():
    """Background pass: re-fetches metadata for history entries with missing/bad titles."""
    global enrich_status
    enrich_status.update({"running": True, "done": 0, "updated": 0, "last_run": datetime.now().isoformat(), "cancel": False})
    try:
        h = load_history()
        # Safety: back up history before any mutations
        import shutil
        hf = HISTORY_FILE()
        if os.path.exists(hf):
            shutil.copy2(hf, hf + ".bak")
        BAD = {"", "untitled", "untitled pdf", "loading..."}
        candidates = [(url, item) for url, item in h.items()
                      if (item.get("title") or "").strip().lower() in BAD]
        enrich_status["total"] = len(candidates)
        def enrich_one(args):
            url, item = args
            is_pdf = url.lower().endswith(".pdf")
            try:
                if is_pdf:
                    title = get_pdf_title(url, "")
                    canonical = url  # PDFs: don't try to canonicalize
                else:
                    meta = fetch_page_content(url)
                    title = (meta or {}).get("title", "").strip() if meta else ""
                    canonical = (meta or {}).get("canonical_url", url) if meta else url
                    # Only accept canonical if same domain (avoid auth redirect traps)
                    from urllib.parse import urlparse
                    if urlparse(canonical).netloc != urlparse(url).netloc:
                        canonical = url
                title_ok = title and title.strip().lower() not in BAD
                return (url, title if title_ok else None, canonical)
            except Exception:
                pass
            return (url, None, url)
        with ThreadPoolExecutor(max_workers=15) as pool:
            futures = {pool.submit(enrich_one, (url, item)): url for url, item in candidates}
            for future in as_completed(futures):
                if enrich_status.get("cancel"):
                    break
                enrich_status["done"] += 1
                result = future.result()
                if not result: continue
                orig_url, title, canonical = result
                if not title and canonical == orig_url: continue
                h2 = load_history()
                if orig_url not in h2: continue
                entry = h2[orig_url]
                changed = False
                if title:
                    entry["title"] = title
                    changed = True
                # Update URL key if canonicalized and not already present
                if canonical != orig_url and canonical not in h2:
                    entry["original_url"] = orig_url  # keep a breadcrumb
                    del h2[orig_url]
                    h2[canonical] = entry
                    changed = True
                if changed:
                    save_history(h2)
                    enrich_status["updated"] += 1
        
        # Autonomous cleanup pass for any remaining orphans
        m_count = consolidate_history()
        enrich_status["updated"] += m_count
    finally:
        enrich_status["running"] = False

bulk_sync_status = {"running": False, "type": None, "total": 0, "done": 0, "succeeded": 0, "failed": 0, "started": None, "reconciled": None, "cancel": False}

def run_bulk_sync(sync_type=None):
    global bulk_sync_status
    # Always reconcile first — prevents re-POSTing items Zotero already has,
    # and re-queuing bookmarks Chrome already has.
    try:
        bulk_sync_status["reconciled"] = reconcile_all()
    except Exception:
        bulk_sync_status["reconciled"] = None
    """Retries Zotero/Bookmark sync for all entries marked as unsynced.
    Saves history incrementally (every 5 items) so the UI can show progress
    via the meta/status endpoints rather than seeing nothing until the end.
    """
    h = load_history()
    targets = [
        (url, item) for url, item in h.items()
        if ((sync_type is None or sync_type == 'zotero') and not item.get("z_synced"))
        or ((sync_type is None or sync_type == 'bookmark') and not item.get("b_synced"))
    ]
    bulk_sync_status.update({
        "running": True, "type": sync_type or "all",
        "total": len(targets), "done": 0, "succeeded": 0, "failed": 0,
        "started": datetime.now().isoformat(), "cancel": False,
    })
    dirty = False
    SAVE_EVERY = 5
    try:
        for i, (url, item) in enumerate(targets, 1):
            if bulk_sync_status.get("cancel"):
                break
            try:
                if (sync_type is None or sync_type == 'zotero') and not item.get("z_synced"):
                    if send_to_zotero(url, item.get("title"), item.get("abstract"), item.get("category"), doi=item.get("doi")):
                        item["z_synced"] = True
                        dirty = True
                        bulk_sync_status["succeeded"] += 1
                    else:
                        bulk_sync_status["failed"] += 1
                if (sync_type is None or sync_type == 'bookmark') and not item.get("b_synced"):
                    if add_chrome_bookmark(url, item.get("title"), item.get("category")):
                        item["b_synced"] = True
                        dirty = True
                        bulk_sync_status["succeeded"] += 1
                    # bookmarks may be queued (Chrome running) — that's not a failure
            except Exception:
                bulk_sync_status["failed"] += 1
            bulk_sync_status["done"] = i
            if dirty and i % SAVE_EVERY == 0:
                save_history(h)
                dirty = False
        if dirty:
            save_history(h)
    finally:
        bulk_sync_status["running"] = False

@app.get("/api/v1/history/sync/status")
def get_bulk_sync_status(): return bulk_sync_status

@app.post("/api/v1/reconcile")
def trigger_reconcile():
    """Manually refresh the Zotero URL cache + scan Chrome bookmarks + flip
    sync flags for anything already present remotely. Then consolidate
    duplicate history rows. Safe to run any time.
    """
    return {"status": "ok", **reconcile_all()}

@app.post("/api/v1/history/sync")
def trigger_sync(type: str = None):
    # run in background
    threading.Thread(target=run_bulk_sync, args=(type,), daemon=True).start()
    return {"status": "started"}

@app.post("/api/v1/history/sync/cancel")
def cancel_bulk_sync():
    bulk_sync_status["cancel"] = True
    return {"status": "cancelling"}

@app.post("/api/v1/history/enrich/cancel")
def cancel_enrich():
    enrich_status["cancel"] = True
    return {"status": "cancelling"}

@app.post("/api/v1/history/sync_single")
def sync_single(url: str, type: str):
    h = load_history()
    if url not in h: return {"status": "error", "message": "not found"}
    item = h[url]
    ok = False
    if type == 'zotero':
        ok = send_to_zotero(url, item.get("title"), item.get("abstract"), item.get("category"), doi=item.get("doi"))
        if ok: item["z_synced"] = True
    elif type == 'bookmark':
        ok = add_chrome_bookmark(url, item.get("title"), item.get("category"))
        if ok: item["b_synced"] = True
    
    if ok:
        _stamp_accountability(item, url, "sync_update", f"manual_sync_{type}")
        _record_accountability_event("sync_update", url, item, source=f"manual_sync_{type}")
        save_history(h)
        return {"status": "ok"}
    return {"status": "error"}

@app.post("/api/v1/history/merge")
def manual_merge():
    merged_count = consolidate_history()
    return {"status": "ok", "merged": merged_count}

@app.get("/api/v1/bookmarks/pending")
def bookmarks_pending():
    """Inspect the queue of bookmark saves waiting for Chrome to close."""
    q = load_json(PENDING_BOOKMARKS_FILE(), [])
    return {
        "pending": q,
        "count": len(q),
        "chrome_running": is_chrome_running()
    }

@app.post("/api/v1/bookmarks/flush")
def bookmarks_flush():
    """Writes all queued bookmarks into Chrome's Bookmarks file.
    REQUIRES Chrome to be closed — otherwise Chrome overwrites the file on exit
    and all edits are lost. Returns an error asking the user to close Chrome
    if any chrome.exe process is detected.
    """
    if is_chrome_running():
        return {
            "status": "error",
            "message": "Close Google Chrome completely (check the system tray) and retry. "
                       "Chrome overwrites its Bookmarks file on exit, so edits made while "
                       "it is running are silently discarded."
        }
    q = load_json(PENDING_BOOKMARKS_FILE(), [])
    saved, failed = 0, 0
    remaining = []
    for item in q:
        ok = _write_bookmark_now(item.get("url",""), item.get("title",""), item.get("category",""))
        if ok:
            saved += 1
            # Flip the history b_synced flag for this URL
            h = load_history()
            if item.get("url") in h:
                h[item["url"]]["b_synced"] = True
                save_history(h)
        else:
            failed += 1
            remaining.append(item)
    save_json(PENDING_BOOKMARKS_FILE(), remaining)
    sweep_status["bookmarks_pending"] = len(remaining)
    return {"status": "ok", "saved": saved, "failed": failed, "remaining": len(remaining)}

def _do_close_synced_tabs_now():
    """Shared implementation used both by the endpoint and the post-sweep
    delayed auto-close thread."""
    try:
        resp = requests.get("http://127.0.0.1:9222/json/list", timeout=10)
        tabs = resp.json()
    except Exception as e:
        return {"status": "error", "message": f"ADB/DevTools not reachable: {e}"}
    h = load_history()
    synced_urls = {}
    # Hard safety gate: only enrol items that pass _safe_to_close. This means
    # uncategorized items are NEVER eligible to be closed by this routine,
    # even if both syncs succeeded somehow.
    for u, item in h.items():
        if _safe_to_close(item):
            synced_urls[u] = (u, item)
            synced_urls[canonicalize_url(u)] = (u, item)
            for k in ("canonical_url", "original_url"):
                if item.get(k): synced_urls[item[k]] = (u, item)
    closed, skipped, failed = 0, 0, 0
    for t in tabs:
        url = t.get("url", "")
        tid = t.get("id")
        if not tid or not url or url.startswith("chrome://"):
            skipped += 1
            continue
        match = synced_urls.get(url) or synced_urls.get(canonicalize_url(url))
        if match:
            history_url, item = match
            try:
                if _close_devtools_tab(tid, history_url, item, "close_synced_tabs"):
                    closed += 1
            except Exception:
                failed += 1
        else:
            skipped += 1
    return {"status": "ok", "closed": closed, "skipped": skipped, "failed": failed, "total": len(tabs)}

@app.get("/api/v1/tabs/inspect")
def inspect_tabs(wake: bool = False):
    """Return every open tab DevTools reports, each tagged with why Panop did
    or didn't act on it. Used to diagnose 'tabs I expected to be saved were
    not'. No mutation. Buckets:
      saved              — URL is already in history
      match_<category>   — would match category <X> on next sweep (not yet saved)
      no_match           — no category pattern matched the URL
      body_required      — domain matches but needs body-keyword check (not run)
      discarded          — DevTools returned an empty/internal URL (Android
                           put the tab to sleep; URL will reappear when active)
      chrome_internal    — chrome:// / about: / devtools:// etc.
    """
    try:
        adb_exe = ensure_adb()
    except Exception as e:
        return {"status": "error", "message": f"ADB unavailable: {e}"}
    tabs = None
    last_err = None
    devtools_alive = False
    browser_info = ""
    for attempt in range(3):
        try:
            subprocess.run([adb_exe, "forward", "--remove", "tcp:9222"], capture_output=True)
        except Exception: pass
        subprocess.run([adb_exe, "forward", "tcp:9222", "localabstract:chrome_devtools_remote"], capture_output=True)
        # Quick liveness probe — /json/version returns instantly even when
        # /json/list is choking on thousands of tabs.
        try:
            v = requests.get("http://127.0.0.1:9222/json/version", timeout=5)
            if v.status_code == 200:
                devtools_alive = True
                try: browser_info = v.json().get("Browser","")
                except Exception: pass
        except Exception as e:
            last_err = f"version probe: {e}"
            time.sleep(1.5)
            continue
        # Now the slow part — give it 3 minutes for very large tab counts
        try:
            resp = requests.get("http://127.0.0.1:9222/json/list", timeout=180)
            if resp.status_code == 200:
                tabs = resp.json()
                break
            last_err = f"HTTP {resp.status_code}"
        except Exception as e:
            last_err = str(e)
        time.sleep(2)
    if tabs is None:
        if devtools_alive:
            return {"status": "error", "message": f"DevTools is alive ({browser_info}) but /json/list took >180s — your phone has too many tabs for Chrome to serialize the list. Try closing some tabs from the phone first, or use Drain mode (which works tab-by-tab and doesn't need a full list)."}
        return {"status": "error", "message": f"DevTools not reachable: {last_err}. Make sure the phone is unlocked, Chrome is in the foreground, and USB Debugging is authorized."}
    woken = 0
    if wake:
        for wake_pass in range(2):
            suspended = [t for t in tabs if not (t.get("url") or "").strip() or t.get("url") == "about:blank"]
            if not suspended: break
            for t in suspended:
                tid = t.get("id")
                if not tid: continue
                try:
                    requests.post(f"http://127.0.0.1:9222/json/activate/{tid}", timeout=3)
                    woken += 1
                except Exception: pass
            time.sleep(2.5)
            try:
                r2 = requests.get("http://127.0.0.1:9222/json/list", timeout=60)
                if r2.status_code == 200: tabs = r2.json()
            except Exception: pass
    h = load_history()
    config = load_config()
    categories = config.get("categories", [])
    strict = get_env().get("strict_domain_scan", True)
    buckets = {"saved": 0, "no_match": 0, "body_required": 0,
               "discarded": 0, "chrome_internal": 0}
    rows = []
    for t in tabs:
        url = (t.get("url") or "").strip()
        title = (t.get("title") or "").strip()
        tid = t.get("id")
        if not url or url == "about:blank":
            buckets["discarded"] += 1
            rows.append({"id": tid, "url": url, "title": title, "status": "discarded",
                         "reason": "Android Chrome suspended this tab — URL will reappear when activated."})
            continue
        if url.startswith("chrome://") or url.startswith("devtools://") or url.startswith("about:") or url.startswith("chrome-native://"):
            buckets["chrome_internal"] += 1
            rows.append({"id": tid, "url": url, "title": title, "status": "chrome_internal", "reason": "Internal Chrome page."})
            continue
        if url in h or canonicalize_url(url) in h:
            buckets["saved"] += 1
            rows.append({"id": tid, "url": url, "title": title, "status": "saved",
                         "reason": "Already in history."})
            continue
        url_lower = url.lower()
        matched = None
        needs_body = False
        matched_layer = None

        override_decision = _classifier_override_decision(url)
        if override_decision and override_decision.get("action") == "block":
            buckets["no_match"] += 1
            rows.append({
                "id": tid, "url": url, "title": title, "status": "no_match",
                "category": "Uncategorized",
                "reason": f"User correction says do not sweep: {override_decision.get('reason', '')}".strip(),
                "decision_source": override_decision.get("source"),
                "decision_confidence": override_decision.get("confidence"),
            })
            continue
        
        # 1. Try smart classifier first (without page meta)
        smart_cat = _classify_tab_candidate(url, None, categories, get_env())
        if smart_cat:
            matched = smart_cat.get("name", "?")
            needs_body = False
            matched_layer = smart_cat.get("_classification_source") or "smart_classifier"
        else:
            # 2. Fall back to old keyword/domain logic
            matched_layer = "inline_rules"
            for cat in categories:
                domains = cat.get("domain_keywords", [])
                tab_group = cat.get("tab_group", "")
                if tab_group and tab_group.lower() not in str(t).lower():
                    continue
                domain_match = any(d.lower() in url_lower for d in domains if d) if domains else True
                if not domain_match and strict and domains:
                    continue
                if domain_match or not domains:
                    matched = cat.get("name", "?")
                    needs_body = bool(cat.get("body_required") or cat.get("body_forbidden"))
                    break
        if matched:
            if needs_body:
                buckets["body_required"] = buckets.get("body_required", 0) + 1
                rows.append({"id": tid, "url": url, "title": title, "status": "needs_body_check",
                             "reason": f"Domain matches '{matched}' but category has body-keyword rules. Next sweep will fetch and check."})
            else:
                if matched_layer == "exact_user_correction":
                    reason_str = f"User correction: would be saved to '{matched}'."
                elif matched_layer == "learned_domain_rule":
                    reason_str = f"Learned from your corrections: would be saved to '{matched}'."
                elif matched_layer == "smart_classifier":
                    reason_str = f"Would be saved to '{matched}' (matched by smart classifier)."
                else:
                    reason_str = f"Would be saved to '{matched}' on next sweep."
                rows.append({"id": tid, "url": url, "title": title, "status": f"match:{matched}",
                             "reason": reason_str,
                             "decision_source": matched_layer,
                             "decision_confidence": smart_cat.get("_classification_confidence") if isinstance(smart_cat, dict) else None})
        else:
            buckets["no_match"] += 1
            rows.append({"id": tid, "url": url, "title": title, "status": "no_match",
                         "reason": "No category's keywords matched this URL."})
    return {"status": "ok", "total": len(tabs), "buckets": buckets, "tabs": rows, "woken": woken}

@app.post("/api/v1/tabs/close-synced")
def close_synced_tabs():
    """Walks the Android device's currently open tabs (via DevTools at :9222)
    and closes every tab whose URL (or its canonical form) is already in
    history with z_synced=true AND b_synced=true. Safe: untouched if either
    flag is missing."""
    if _manual_vetting_required():
        return {"status": "blocked", "closed": 0, "message": "Manual vetting is required before Egon may close already-synced tabs."}
    return _do_close_synced_tabs_now()

# ── Sequential wake-save-close drain for thousands of suspended tabs ─────────
drain_status = {"running": False, "processed": 0, "saved": 0, "closed": 0,
                "skipped": 0, "remaining": 0, "current_url": "", "cancel": False,
                "last_error": "", "started_at": None, "finished_at": None,
                "phase": "idle", "total_initial": 0}

def _drain_state_file():
    return os.path.join(OUTPUT_DIR(), "panop_drain_state.json")

def _load_drain_state():
    return load_json(_drain_state_file(), {"processed_ids": []})

def _save_drain_state(s):
    save_json(_drain_state_file(), s)

def _safe_to_close(item):
    """HARD SAFETY GUARD. Refuses to close a tab unless:
        (1) it has a real category match (not 'uncategorized' / empty)
        (2) AND the item has been backed up to BOTH Zotero and Chrome Bookmarks
    No env flag, no future code path, can override this. A tab that does not
    pass this gate is left OPEN on the phone, no matter what."""
    if _manual_vetting_required(): return False
    if not item: return False
    cat_id = (item.get("cat_id") or "").strip().lower()
    if not cat_id or cat_id == "uncategorized": return False
    return bool(item.get("z_synced")) and bool(item.get("b_synced"))

def _append_close_audit(entry):
    try:
        path = CLOSE_AUDIT_FILE()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass

def _close_devtools_tab(tid, url, item, source):
    """Close one Android Chrome DevTools target with a durable audit trail."""
    env = get_env()
    safe = _safe_to_close(item)
    backup_proof = _verify_close_backups(url, item) if safe else {"ok": False, "skipped": "safe_to_close_false"}
    entry = {
        "ts": datetime.now().isoformat(),
        "source": source,
        "tab_id": tid,
        "url": url,
        "category": (item or {}).get("category"),
        "cat_id": (item or {}).get("cat_id"),
        "z_synced": bool((item or {}).get("z_synced")),
        "b_synced": bool((item or {}).get("b_synced")),
        "safe_to_close": bool(safe),
        "backup_proof": backup_proof,
        "manual_vetting_required": bool(_manual_vetting_required(env)),
        "close_tabs_after_save": bool(env.get("close_tabs_after_save")),
        "enable_autonomous_sweep": bool(env.get("enable_autonomous_sweep")),
        "closed": False,
    }
    if not safe:
        entry["blocked_reason"] = "safe_to_close_false"
        _append_close_audit(entry)
        _record_accountability_event("close_blocked", url, item, source=source, close=entry)
        return False
    if not backup_proof.get("ok"):
        entry["blocked_reason"] = "missing_verified_bookmark_or_zotero"
        _append_close_audit(entry)
        _record_accountability_event("close_blocked", url, item, source=source, close=entry)
        return False
    try:
        resp = requests.post(f"http://127.0.0.1:9222/json/close/{tid}", timeout=5)
        entry["response_status"] = resp.status_code
        entry["closed"] = 200 <= resp.status_code < 300
        return bool(entry["closed"])
    except Exception as e:
        entry["error"] = str(e)[:300]
        return False
    finally:
        _append_close_audit(entry)
        _record_accountability_event("close_attempt", url, item, source=source, close=entry)

def _classify_tab_candidate(url, page_meta, categories, env):
    """Classifies a tab candidate using Egon's smart layered classifier,
    falling back to inline domain/keyword matching if needed.
    """
    classify_url = _resolve_terminal_tab_url(url, env)
    try:
        decision = _classifier_override_decision(classify_url) or _classifier_override_decision(url)
        if decision:
            if decision.get("action") == "block":
                return None
            matched = _category_by_id(categories, decision.get("category_id"))
            if matched:
                out = dict(matched)
                out["_classification_source"] = decision.get("source")
                out["_classification_confidence"] = decision.get("confidence")
                out["_classification_reason"] = decision.get("reason")
                return out
    except Exception:
        pass

    try:
        import sys, os
        _parent = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        if _parent not in sys.path:
            sys.path.insert(0, _parent)
        import lib.classifier as classifier
        res = classifier.classify(classify_url, page_meta or {})
        if res.action == "match" and res.category:
            matched = next((c for c in categories if c.get("id") == res.category), None)
            if matched:
                out = dict(matched)
                out["_classification_source"] = "smart_classifier"
                out["_classification_layer"] = res.layer
                out["_classification_confidence"] = res.confidence
                out["_classification_reason"] = (res.evidence or {}).get("reason", "")
                return out
    except Exception:
        pass

    url_lower = classify_url.lower()
    for cat in categories:
        domains = cat.get("domain_keywords", [])
        if domains and not any(d.lower() in url_lower for d in domains if d):
            continue
        if cat.get("body_required") or cat.get("body_forbidden"):
            page = ((page_meta or {}).get("text") or (page_meta or {}).get("title") or "") + " " + ((page_meta or {}).get("abstract") or "")
            pl = page.lower()
            req = [w.lower() for w in cat.get("body_required", []) if w]
            forb = [w.lower() for w in cat.get("body_forbidden", []) if w]
            mode = cat.get("body_required_mode", "ALL")
            if req:
                ok = all(w in pl for w in req) if mode == "ALL" else any(w in pl for w in req)
                if not ok: continue
            if forb and any(w in pl for w in forb): continue
        out = dict(cat)
        out["_classification_source"] = "inline_rules"
        out["_classification_confidence"] = 0.65
        out["_classification_reason"] = "Matched configured inline rule."
        return out
    return None

def _drain_classify_and_save(url, title, categories, env, tid):
    """Returns (saved_bool, closed_bool). Shared by both phases."""
    h = load_history()
    terminal_url = _resolve_terminal_tab_url(url, env)
    storage_url = canonicalize_url(terminal_url) or terminal_url or url
    if storage_url in h or url in h:
        item = h.get(storage_url) or h.get(url) or {}
        if env.get("close_tabs_after_save") and not _manual_vetting_required(env) and _safe_to_close(item):
            try:
                if _close_devtools_tab(tid, storage_url, item, "drain_existing_synced"):
                    return False, True
            except Exception: pass
        return False, False
    try: meta_p = fetch_page_content(terminal_url) or {}
    except Exception: meta_p = {}
    
    matched = _classify_tab_candidate(terminal_url, meta_p, categories, env)
    
    if not matched:
        if env.get("catch_uncategorized"):
            matched = {"id": "uncategorized", "name": "Uncategorized", "dest_folder": "Uncategorized"}
        else:
            return False, False
    try:
        z_ok = send_to_zotero(storage_url, title or terminal_url, "", matched["name"], doi="")
        b_ok = add_chrome_bookmark(storage_url, title or terminal_url, matched["name"])
        h2 = load_history()
        h2[storage_url] = {
            "title": title or terminal_url, "category": matched["name"],
            "cat_id": matched["id"], "date": datetime.now().isoformat(),
            "abstract": "", "canonical_url": storage_url,
            "original_url": url if url != storage_url else None,
            "doi": "", "ai_learned": False, "file": "",
            "z_synced": z_ok, "b_synced": b_ok
        }
        _stamp_accountability(
            h2[storage_url], storage_url, "history_upsert", "drain",
            classification={
                "cat_id": matched.get("id"),
                "category": matched.get("name"),
                "source": matched.get("_classification_source") or "configured_rules",
                "confidence": matched.get("_classification_confidence"),
                "reason": matched.get("_classification_reason"),
            },
        )
        _record_accountability_event("history_upsert", storage_url, h2[storage_url], source="drain")
        # Science-News 2nd stage — explode a digest into its contained items
        # (writes into h2; persisted by save_history below). No-op for stories.
        if matched.get("id") == "science_news":
            try:
                ss = second_stage_extract(storage_url, env, categories, history=h2)
                if ss.get("saved"):
                    h2[storage_url]["second_stage"] = {"found": ss["found"],
                        "saved": ss["saved"], "by_cat": ss["by_cat"]}
            except Exception:
                pass
        save_history(h2)
        closed = False
        # Hard safety gate: never close uncategorized tabs, ever.
        if env.get("close_tabs_after_save") and not _manual_vetting_required(env) and _safe_to_close(h2[storage_url]):
            try:
                closed = _close_devtools_tab(tid, storage_url, h2[storage_url], "drain_after_save")
            except Exception: pass
        return True, closed
    except Exception as e:
        drain_status["last_error"] = str(e)[:200]
        return False, False

def _process_all_tabs_loop(resume=True):
    """Two-phase drain optimized for thousands of suspended tabs.
       Phase A — process every tab that ALREADY has a visible URL (1-2s each).
       Phase B — batch-wake suspended tabs in groups, then process those that woke up.
       State (processed_ids) persisted to disk so the user can stop and resume."""
    try:
        adb_exe = ensure_adb()
        # Re-establish forward + initial /json/list
        initial = None
        for _ in range(4):
            try: subprocess.run([adb_exe, "forward", "--remove", "tcp:9222"], capture_output=True)
            except Exception: pass
            subprocess.run([adb_exe, "forward", "tcp:9222", "localabstract:chrome_devtools_remote"], capture_output=True)
            try:
                resp = requests.get("http://127.0.0.1:9222/json/list", timeout=180)
                if resp.status_code == 200:
                    initial = resp.json()
                    break
            except Exception: pass
            time.sleep(2)
        if initial is None:
            drain_status["last_error"] = "DevTools /json/list unreachable — bring Chrome to foreground on phone."
            return

        config = load_config()
        env = get_env()
        categories = config.get("categories", [])

        state = _load_drain_state() if resume else {"processed_ids": []}
        processed_ids = set(state.get("processed_ids", []))
        drain_status["total_initial"] = len(initial)

        def _is_real_url(u):
            return u and u != "about:blank" and not u.startswith("chrome://") \
                and not u.startswith("devtools://") and not u.startswith("chrome-native://")

        def _process_tab(tid, url, title):
            drain_status["current_url"] = (url or "")[:120]
            saved, closed = _drain_classify_and_save(url, title, categories, env, tid)
            if saved: drain_status["saved"] += 1
            if closed: drain_status["closed"] += 1
            if not saved and not closed:
                drain_status["skipped"] += 1
            drain_status["processed"] += 1
            processed_ids.add(tid)
            # Persist every 10 tabs
            if drain_status["processed"] % 10 == 0:
                _save_drain_state({"processed_ids": list(processed_ids)})

        # ── PHASE A: tabs whose URL is already visible (no wake needed) ──
        drain_status["phase"] = "A: visible URLs"
        phase_a = [(t.get("id"), t.get("url",""), t.get("title","")) for t in initial
                   if t.get("id") and t.get("id") not in processed_ids and _is_real_url((t.get("url") or "").strip())]
        drain_status["remaining"] = len(phase_a) + sum(1 for t in initial if t.get("id") and t.get("id") not in processed_ids and not _is_real_url((t.get("url") or "").strip()))
        for tid, url, title in phase_a:
            if drain_status["cancel"]:
                drain_status["last_error"] = "Cancelled — state saved, click Drain again to resume"
                _save_drain_state({"processed_ids": list(processed_ids)})
                return
            _process_tab(tid, url.strip(), title.strip())
            drain_status["remaining"] = max(0, drain_status["remaining"] - 1)

        _save_drain_state({"processed_ids": list(processed_ids)})

        # ── PHASE B: batch-wake suspended tabs ──
        drain_status["phase"] = "B: waking suspended"
        BATCH = 40
        suspended = [t.get("id") for t in initial
                     if t.get("id") and t.get("id") not in processed_ids]
        # Filter out IDs we already touched in phase A (just in case)
        suspended = [tid for tid in suspended if tid not in processed_ids]

        for batch_start in range(0, len(suspended), BATCH):
            if drain_status["cancel"]:
                drain_status["last_error"] = "Cancelled — state saved, click Drain again to resume"
                _save_drain_state({"processed_ids": list(processed_ids)})
                return
            batch = suspended[batch_start:batch_start + BATCH]
            woke_info = {}
            # Up to 3 activation rounds — Chrome Android often silently drops
            # /json/activate calls when many arrive at once. Re-sending the
            # activate for stragglers recovers most of them.
            for round_idx in range(3):
                # Activate (every round, for whoever is still not woken)
                to_activate = [tid for tid in batch if tid not in woke_info]
                if not to_activate: break
                for tid in to_activate:
                    try: requests.post(f"http://127.0.0.1:9222/json/activate/{tid}", timeout=3)
                    except Exception: pass
                # Initial wait scales up per round: 4s, 6s, 8s
                time.sleep(4 + round_idx * 2)
                # Poll up to 4 times with 2s between
                for poll in range(4):
                    try:
                        r = requests.get("http://127.0.0.1:9222/json/list", timeout=120)
                        if r.status_code != 200: break
                        by_id = {t.get("id"): t for t in r.json()}
                        pending = []
                        for tid in batch:
                            if tid in woke_info: continue
                            t = by_id.get(tid)
                            if not t:
                                woke_info[tid] = (None, None, None)  # gone
                                continue
                            u = (t.get("url") or "").strip()
                            if _is_real_url(u):
                                woke_info[tid] = (tid, u, (t.get("title") or "").strip())
                            else:
                                pending.append(tid)
                        if not pending: break
                        time.sleep(2)
                    except Exception:
                        time.sleep(1)
                # If nobody pending left, we're done with this batch
                if all(tid in woke_info for tid in batch): break
            # Process what we got
            for tid in batch:
                if drain_status["cancel"]:
                    drain_status["last_error"] = "Cancelled — state saved, click Drain again to resume"
                    _save_drain_state({"processed_ids": list(processed_ids)})
                    return
                info = woke_info.get(tid)
                if not info or not info[1]:
                    # Could not wake — count as skip but DO NOT mark processed
                    # so the next drain run can retry it.
                    drain_status["skipped"] += 1
                    drain_status["processed"] += 1
                    drain_status["remaining"] = max(0, drain_status["remaining"] - 1)
                    continue
                _, url, title = info
                _process_tab(tid, url, title)
                drain_status["remaining"] = max(0, drain_status["remaining"] - 1)
            _save_drain_state({"processed_ids": list(processed_ids)})

        drain_status["finished_at"] = datetime.now().isoformat()
        drain_status["phase"] = "done"
        # Wipe state on clean finish so a future Drain starts fresh
        _save_drain_state({"processed_ids": []})
    except Exception as e:
        drain_status["last_error"] = str(e)[:200]
    finally:
        drain_status["running"] = False
        drain_status["current_url"] = ""

@app.post("/api/v1/tabs/drain")
def drain_all_tabs():
    if drain_status["running"]:
        return {"status": "already_running"}
    drain_status.update({"running": True, "processed": 0, "saved": 0, "closed": 0,
                         "skipped": 0, "remaining": 0, "current_url": "",
                         "cancel": False, "last_error": "",
                         "started_at": datetime.now().isoformat(), "finished_at": None})
    threading.Thread(target=_process_all_tabs_loop, daemon=True).start()
    return {"status": "started"}

@app.get("/api/v1/tabs/drain/status")
def drain_get_status():
    return drain_status

@app.post("/api/v1/phone/keep_awake")
def phone_keep_awake():
    """Sets Android screen-off timeout to ~25 days. Works over USB AND wireless
    ADB. Records the previous value so we can restore it. Also disables the
    lock screen briefly via stayon-while-charging if USB is detected."""
    try:
        adb_exe = ensure_adb()
    except Exception as e:
        return {"status": "error", "message": f"ADB unavailable: {e}"}
    # Read previous timeout
    prev = ""
    try:
        r = subprocess.run([adb_exe, "shell", "settings", "get", "system", "screen_off_timeout"],
                           capture_output=True, text=True, timeout=10)
        prev = (r.stdout or "").strip()
    except Exception: pass
    # Save it
    env = get_env()
    env["_phone_prev_screen_timeout"] = prev or "60000"
    save_env(env)
    # Set to ~25 days (max int / 2)
    try:
        subprocess.run([adb_exe, "shell", "settings", "put", "system",
                        "screen_off_timeout", "1800000000"],
                       capture_output=True, timeout=10)
    except Exception as e:
        return {"status": "error", "message": f"Failed to set timeout: {e}"}
    # Also enable stayon-while-USB-or-AC-or-wireless plugged
    try:
        subprocess.run([adb_exe, "shell", "svc", "power", "stayon", "true"],
                       capture_output=True, timeout=10)
    except Exception: pass
    return {"status": "ok", "previous_timeout_ms": prev,
            "message": "Phone screen will stay on. Click Restore Sleep when done."}

@app.post("/api/v1/phone/pair")
def phone_pair(host: str = "", port: int = 0, code: str = ""):
    """One-shot Android 11+ wireless-debugging pair.
    Required: host (phone IP), port (the 5-digit port shown under
    'Pair device with pairing code' on the phone — NOT the main port),
    code (the 6-digit pairing code shown alongside it).
    After successful pair the persistent 'connect' port (visible on the main
    Wireless Debugging screen) becomes usable by adb connect for the lifetime
    of that wireless-debug session."""
    host = (host or "").strip()
    code = (code or "").strip()
    if not host or not port or not code:
        return {"status": "error", "message": "host, port and code are all required"}
    try:
        adb_exe = ensure_adb()
    except Exception as e:
        return {"status": "error", "message": f"ADB unavailable: {e}"}
    target = f"{host}:{port}"
    try:
        # adb pair reads the code from stdin
        p = subprocess.run([adb_exe, "pair", target],
                           input=(code + "\n"), capture_output=True, text=True, timeout=20)
        out = (p.stdout or "") + (p.stderr or "")
        ok = ("Successfully paired" in out) or ("paired to" in out.lower())
        return {"status": "ok" if ok else "error",
                "paired": ok, "log": out.strip(),
                "hint": None if ok else
                  "Make sure (a) the port is the 5-digit one under 'Pair device "
                  "with pairing code' (NOT the main connect port), and (b) the "
                  "6-digit code matches what's currently on screen. The pair "
                  "screen and its code expire after ~30s — try again if so."}
    except Exception as e:
        return {"status": "error", "paired": False, "message": str(e)}

_adb_lock = threading.Lock()  # serialise adb subprocess calls

def _adb_mdns_discover(adb_exe, expected_host=None):
    """Use adb's built-in mDNS browser to find the *current* connect endpoint
    advertised by Android. Android Wireless Debugging publishes a service of
    type `_adb-tls-connect._tcp` whenever WD is enabled. The IP can stay the
    same while the port rotates — mDNS always has the live port.
    Returns 'host:port' string or None.
    Optionally filters by expected_host (e.g. existing Trusted IP)."""
    try:
        p = subprocess.run([adb_exe, "mdns", "services"],
                           capture_output=True, text=True, timeout=8)
        out = (p.stdout or "") + "\n" + (p.stderr or "")
    except Exception:
        return None
    # Lines look like: "adb-XYZ._adb-tls-connect._tcp\t192.168.1.50:38123"
    # or sometimes: "adb-XYZ\t_adb-tls-connect._tcp.\t192.168.1.50:38123"
    candidates = []
    for line in out.splitlines():
        if "_adb-tls-connect" not in line: continue
        # Find anything that looks like IP:port at the end
        m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3}):(\d{2,5})", line)
        if m:
            ip, port = m.group(1), m.group(2)
            candidates.append(f"{ip}:{port}")
    if not candidates: return None
    if expected_host:
        for c in candidates:
            if c.startswith(expected_host + ":"): return c
    return candidates[0]

def _adb_try_reconnect(adb_exe):
    """Try wireless reconnect using configured wireless_ips, then fall back to
    mDNS discovery to handle Android's rotating connect port. Returns True if
    a device ends up in 'device' state. Persists any newly-discovered port
    back to wireless_ips so subsequent calls are fast."""
    with _adb_lock:
        try:
            ready, _u, _o = _adb_list_devices(adb_exe)
            if ready: return True
        except Exception: pass
        try:
            config = load_config()
        except Exception:
            config = {}
        configured = list(config.get("wireless_ips", []) or [])
        # First try: saved entries
        for ip in configured:
            ip = (ip or "").strip()
            if not ip: continue
            target = ip if ":" in ip else f"{ip}:5555"
            try:
                subprocess.run([adb_exe, "connect", target], capture_output=True, timeout=8)
            except Exception: pass
        try:
            ready, _u, _o = _adb_list_devices(adb_exe)
            if ready: return True
        except Exception: pass
        # Second try: mDNS discovery. Use the host of the first saved entry
        # as a preference filter (in case multiple phones are paired).
        expected_host = None
        if configured:
            first = (configured[0] or "").strip()
            if first: expected_host = first.split(":")[0]
        discovered = _adb_mdns_discover(adb_exe, expected_host)
        if discovered:
            try:
                subprocess.run([adb_exe, "connect", discovered],
                               capture_output=True, timeout=8)
            except Exception: pass
            # Persist for next time
            try:
                config = load_config()
                ips = config.get("wireless_ips", []) or []
                # Replace any entry sharing the same host with the discovered one,
                # otherwise prepend.
                host = discovered.split(":")[0]
                ips = [x for x in ips if not (x or "").startswith(host + ":")]
                ips.insert(0, discovered)
                config["wireless_ips"] = ips
                save_config(config)
            except Exception: pass
        try:
            ready, _u, _o = _adb_list_devices(adb_exe)
            return bool(ready)
        except Exception:
            return False

def _auto_rebuild_wireless_from_usb(adb_exe, usb_serial) -> tuple[bool, str, list[str]]:
    """Configures a USB-connected phone for Wireless ADB automatically.
    Returns (success, message, logs).
    """
    logs = [f"Auto-rebuild triggered for USB device: {usb_serial}"]
    try:
        # 1. Switch to tcpip 5555
        r = subprocess.run([adb_exe, "-s", usb_serial, "tcpip", "5555"], capture_output=True, text=True, timeout=10)
        logs.append(f"Ran tcpip 5555: {(r.stdout or r.stderr or '').strip()}")
        
        # 2. Get device IP address
        r_ip = subprocess.run([adb_exe, "-s", usb_serial, "shell", "ip", "route", "get", "1.1.1.1"], capture_output=True, text=True, timeout=8)
        out_ip = r_ip.stdout or ""
        m = re.search(r"src\s+(\d{1,3}(?:\.\d{1,3}){3})", out_ip)
        ip = m.group(1) if m else None
        
        if ip:
            target = f"{ip}:5555"
            logs.append(f"Retrieved device IP: {ip}. Connecting to {target}...")
            # 3. Connect wirelessly
            r_conn = subprocess.run([adb_exe, "connect", target], capture_output=True, text=True, timeout=8)
            logs.append(f"Connecting to {target}: {(r_conn.stdout or r_conn.stderr or '').strip()}")
            
            # 4. Save to config
            try:
                config = load_config()
                ips = config.get("wireless_ips", []) or []
                ips = [x for x in ips if not (x or "").startswith(ip + ":")]
                ips.insert(0, target)
                config["wireless_ips"] = ips
                save_config(config)
                logs.append(f"Saved {target} to config.")
            except Exception as e:
                logs.append(f"Failed to save IP to config: {e}")
                
            # Verify connection
            ready, _, _ = _adb_list_devices(adb_exe)
            if any(target in d for d in ready):
                return True, f"Successfully built and connected to wireless ADB at {target}!", logs
            else:
                return False, f"Switched to TCP/IP mode but failed to connect to {target}.", logs
        else:
            # Fallback: try to resolve IP using getprop or ip addr show
            r_ip2 = subprocess.run([adb_exe, "-s", usb_serial, "shell", "ip", "addr", "show", "wlan0"], capture_output=True, text=True, timeout=8)
            out_ip2 = r_ip2.stdout or ""
            m2 = re.search(r"inet\s+(\d{1,3}(?:\.\d{1,3}){3})", out_ip2)
            ip2 = m2.group(1) if m2 else None
            if ip2:
                target2 = f"{ip2}:5555"
                logs.append(f"Retrieved device IP from wlan0: {ip2}. Connecting to {target2}...")
                subprocess.run([adb_exe, "connect", target2], capture_output=True, timeout=8)
                return True, f"Connected to wireless ADB at {target2} (wlan0)!", logs
            
            return False, "Could not retrieve IP address from device shell via ip route or ip addr.", logs
    except Exception as e:
        return False, f"Error during auto-rebuild: {e}", logs

# Background watchdog that keeps the wireless connection alive.
_usb_rebuild_cooldown = {} # usb_serial -> last_try_timestamp
_watchdog_thread = None
def _adb_watchdog_loop():
    """Polls every 6 s. If the device is not in 'device' state, fire a
    reconnect attempt. Runs forever as a daemon thread."""
    while True:
        try:
            adb_exe = ensure_adb()
            ready, _u, _o = _adb_list_devices(adb_exe)
            
            # Check if we have USB devices but no wireless devices
            usb_devices = [d for d in ready if ":" not in d]
            wireless_devices = [d for d in ready if ":" in d]
            
            if usb_devices and not wireless_devices:
                # Automate USB-to-Wireless transition with 5-min cooldown!
                usb_serial = usb_devices[0]
                now_ts = time.time()
                if now_ts - _usb_rebuild_cooldown.get(usb_serial, 0) > 300:
                    _usb_rebuild_cooldown[usb_serial] = now_ts
                    _auto_rebuild_wireless_from_usb(adb_exe, usb_serial)
            elif not ready:
                _adb_try_reconnect(adb_exe)
        except Exception:
            pass
        time.sleep(6)

def _start_adb_watchdog():
    global _watchdog_thread
    if _watchdog_thread and _watchdog_thread.is_alive(): return
    _watchdog_thread = threading.Thread(target=_adb_watchdog_loop, daemon=True)
    _watchdog_thread.start()

@app.post("/api/v1/phone/reconnect")
def phone_reconnect():
    try:
        adb_exe = ensure_adb()
    except Exception as e:
        return {"status": "error", "message": f"ADB unavailable: {e}"}
    # Hard restart the adb server — fixes most "connects then drops" cases
    try:
        subprocess.run([adb_exe, "kill-server"], capture_output=True, timeout=5)
    except Exception: pass
    try:
        subprocess.run([adb_exe, "start-server"], capture_output=True, timeout=10)
    except Exception: pass
    # Collect per-IP connect output so we can surface it to the UI
    try:
        config = load_config()
    except Exception:
        config = {}
    connect_logs = []
    for ip in config.get("wireless_ips", []) or []:
        ip = (ip or "").strip()
        if not ip: continue
        target = ip if ":" in ip else f"{ip}:5555"
        try:
            r = subprocess.run([adb_exe, "connect", target],
                               capture_output=True, text=True, timeout=10)
            connect_logs.append(f"{target}: {(r.stdout or r.stderr or '').strip()}")
        except Exception as e:
            connect_logs.append(f"{target}: {e}")
    # Immediate check
    try:
        ready, unauth, offline = _adb_list_devices(adb_exe)
    except Exception:
        ready, unauth, offline = [], [], []
    # Re-check after 2.5s to detect "connects then drops"
    time.sleep(2.5)
    try:
        ready2, unauth2, offline2 = _adb_list_devices(adb_exe)
    except Exception:
        ready2, unauth2, offline2 = [], [], []
    dropped = bool(ready) and not bool(ready2)
    return {"status": "ok" if ready2 else "error",
            "connected": bool(ready2),
            "dropped_after_connect": dropped,
            "devices": ready2, "unauthorized": unauth2, "offline": offline2,
            "connect_log": connect_logs,
            "hint": ("Port is probably the PAIR port, not the CONNECT port. "
                     "On Android: Developer options › Wireless debugging › "
                     "the 'IP address & Port' on the MAIN screen (not the "
                     "'Pair device with pairing code' one).") if dropped else None}


@app.post("/api/v1/phone/usb_diagnose")
def phone_usb_diagnose():
    try:
        adb_exe = ensure_adb()
    except Exception as e:
        return {"status": "error", "message": f"ADB unavailable: {e}", "logs": []}
        
    logs = []
    
    # 1. Kill and restart ADB server to refresh USB connections
    try:
        subprocess.run([adb_exe, "kill-server"], capture_output=True, timeout=5)
        subprocess.run([adb_exe, "start-server"], capture_output=True, timeout=10)
        logs.append("Restarted ADB server.")
    except Exception as e:
        logs.append(f"Failed to restart ADB server: {e}")

    # 2. Get adb devices
    try:
        ready, unauth, offline = _adb_list_devices(adb_exe)
    except Exception as e:
        ready, unauth, offline = [], [], []
        logs.append(f"Failed to list ADB devices: {e}")

    # 3. Handle unauthorized devices
    if unauth:
        serial = unauth[0]
        logs.append(f"Found unauthorized device in ADB: {serial}")
        return {
            "status": "warning",
            "connected": False,
            "message": (
                f"Phone [{serial}] is connected, but UNAUTHORIZED.\n\n"
                "Action Required:\n"
                "1. Unlock your phone's screen.\n"
                "2. Look for a popup dialog saying 'Allow USB debugging?' or 'Allow access to device data?'.\n"
                "3. Tick 'Always allow from this computer' and tap 'Allow' or 'OK'.\n"
                "4. After authorizing, click 'Diagnose USB' again."
            ),
            "logs": logs
        }

    # 4. Handle offline devices
    if offline:
        serial = offline[0]
        logs.append(f"Found offline device in ADB: {serial}")
        return {
            "status": "warning",
            "connected": False,
            "message": (
                f"Phone [{serial}] is connected, but reports as OFFLINE in ADB.\n\n"
                "Troubleshooting Steps:\n"
                "1. Unplug the USB cable from the phone and plug it back in.\n"
                "2. Try a different USB cable or a different USB port on your PC.\n"
                "3. Toggle Developer Options off and on, or toggle USB Debugging off and on in Settings.\n"
                "4. Restart the phone and try again."
            ),
            "logs": logs
        }

    # 5. Handle ready devices
    if ready:
        serial = ready[0]
        logs.append(f"Found ready ADB device: {serial}")
        
        # Check if it is a USB device or wireless
        if ":" not in serial:
            # It's a USB device! Try to set tcpip mode and connect wirelessly
            success, msg, run_logs = _auto_rebuild_wireless_from_usb(adb_exe, serial)
            return {
                "status": "ok" if success else "error",
                "connected": success,
                "message": msg,
                "logs": logs + run_logs
            }
        else:
            # Already connected wirelessly
            return {
                "status": "ok",
                "connected": True,
                "message": f"Phone is already connected wirelessly via {serial}.",
                "logs": logs
            }
            
    # 6. If no device found in ADB, check Windows PnP for USB devices
    pnp_devices = []
    if sys.platform == "win32":
        try:
            cmd = ["powershell", "-NoProfile", "-Command",
                   "Get-PnpDevice | Where-Object { $_.Present -eq $true -and $_.FriendlyName -match 'android|motorola|samsung|google|phone' } | Select-Object -ExpandProperty FriendlyName"]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=12)
            for line in (r.stdout or "").splitlines():
                line = line.strip()
                if line and "microphone" not in line.lower():
                    pnp_devices.append(line)
        except Exception as e:
            logs.append(f"Failed to query Windows PnP via PowerShell: {e}")

    # Build diagnostic hint for PnP devices
    if pnp_devices:
        dev_names = ", ".join(pnp_devices[:3])
        message = (
            f"Windows detected USB device(s) [{dev_names}], but ADB does not see them.\n\n"
            "Diagnosis & Actions:\n"
            "1. USB Debugging might be disabled on your phone. Go to Settings > System > Developer Options and verify 'USB Debugging' is enabled.\n"
            "2. USB connection mode might be set to 'Charge only'. Swipe down on your phone, tap the USB/Charging notification, and select 'File Transfer', 'MTP', or 'MIDI'.\n"
            "3. The ADB interface driver might be missing or faulty in Windows Device Manager. You may need to download/install the OEM/Motorola USB Driver."
        )
    else:
        message = (
            "No Android phone detected via USB or ADB.\n\n"
            "Diagnosis & Actions:\n"
            "1. Check if the cable is securely connected to both the phone and PC.\n"
            "2. Make sure you are using a data-transfer USB cable, not a charging-only cable.\n"
            "3. Try toggling Developer Options and USB Debugging OFF and ON again on your phone.\n"
            "4. Add your phone's Wireless Debugging IP (e.g. 192.168.1.42:5555) in Egon System Settings."
        )

    return {
        "status": "error",
        "connected": False,
        "message": message,
        "logs": logs
    }


_awake_cache = {"at": 0, "val": None}

@app.get("/api/v1/phone/awake_status")
def phone_awake_status():
    # Cache result for 5 s — UI polls every 10 s, this avoids two overlapping
    # adb-shell calls when reconnect + poll fire near each other (which is
    # what makes the phone "drop after one click").
    if time.time() - _awake_cache["at"] < 5 and _awake_cache["val"] is not None:
        return _awake_cache["val"]
    try:
        adb_exe = ensure_adb()
    except Exception as e:
        out = {"awake": False, "available": False, "message": str(e)}
        _awake_cache.update(at=time.time(), val=out); return out
    # Use a single locked transaction so this can't race the watchdog
    with _adb_lock:
        try:
            d = subprocess.run([adb_exe, "devices"], capture_output=True, text=True, timeout=5)
            lines = [l.strip() for l in (d.stdout or "").splitlines() if l.strip()]
            has_device = any(l.endswith("device") for l in lines[1:])
        except Exception as e:
            out = {"awake": False, "available": False, "message": f"devices: {e}"}
            _awake_cache.update(at=time.time(), val=out); return out
    if not has_device:
        # Let the watchdog handle reconnection in the background; don't
        # trigger our own here (avoids double-reconnects on every poll).
        out = {"awake": False, "available": False, "message": "no device"}
        _awake_cache.update(at=time.time(), val=out); return out
    try:
        with _adb_lock:
            r = subprocess.run([adb_exe, "shell", "settings", "get", "system", "screen_off_timeout"],
                               capture_output=True, text=True, timeout=10)
        val = (r.stdout or "").strip()
        err = (r.stderr or "").strip()
        if r.returncode != 0 or "no devices" in err.lower() or "error" in err.lower():
            out = {"awake": False, "available": False, "message": err or "shell failed"}
        else:
            try:
                ms = int(val)
                out = {"awake": ms > 3600000, "available": True, "timeout_ms": ms}
            except ValueError:
                out = {"awake": False, "available": True, "timeout_ms": None, "raw": val}
        _awake_cache.update(at=time.time(), val=out); return out
    except Exception as e:
        out = {"awake": False, "available": False, "message": str(e)}
        _awake_cache.update(at=time.time(), val=out); return out

@app.post("/api/v1/phone/restore_sleep")
def phone_restore_sleep():
    try:
        adb_exe = ensure_adb()
    except Exception as e:
        return {"status": "error", "message": f"ADB unavailable: {e}"}
    env = get_env()
    prev = env.get("_phone_prev_screen_timeout") or "60000"  # 60s default
    try:
        subprocess.run([adb_exe, "shell", "settings", "put", "system",
                        "screen_off_timeout", str(prev)], capture_output=True, timeout=10)
    except Exception: pass
    try:
        subprocess.run([adb_exe, "shell", "svc", "power", "stayon", "false"],
                       capture_output=True, timeout=10)
    except Exception: pass
    return {"status": "ok", "restored_to_ms": prev}

@app.post("/api/v1/tabs/drain/cancel")
def drain_cancel():
    drain_status["cancel"] = True
    return {"status": "cancelling"}

@app.post("/api/v1/bookmarks/reset-flags")
def bookmarks_reset_flags():
    """Scan actual Chrome bookmarks file, and for every history row whose
    b_synced=true is NOT backed by a real bookmark, flip it back to false so
    the next bulk sync re-queues it. Use this after a partial/broken flush."""
    bk_urls = scan_chrome_bookmarks_for_panop()
    h = load_history()
    reset = 0
    for u, item in h.items():
        if not item.get("b_synced"):
            continue
        canon = canonicalize_url(u)
        cands = {u, canon, item.get("canonical_url"), item.get("original_url")}
        cands.discard(None)
        if not (cands & bk_urls):
            item["b_synced"] = False
            reset += 1
    if reset:
        save_history(h)
    return {"status": "ok", "reset": reset, "bookmark_urls_seen": len(bk_urls)}

@app.post("/api/v1/bookmarks/ack")
def bookmarks_ack(payload: Dict[str, Any]):
    """Called by the Panop Chrome extension after it writes bookmarks via the
    chrome.bookmarks API. Removes acknowledged items from the pending queue
    and flips b_synced=true on the matching history rows.
    payload: {"items": [{"url": ..., "category": ...}, ...]}
    """
    items = payload.get("items") or []
    ack_keys = {(i.get("url",""), i.get("category","")) for i in items}
    with bookmarks_lock:
        q = load_json(PENDING_BOOKMARKS_FILE(), [])
        remaining = [x for x in q if (x.get("url",""), x.get("category","")) not in ack_keys]
        save_json(PENDING_BOOKMARKS_FILE(), remaining)
        sweep_status["bookmarks_pending"] = len(remaining)
    h = load_history()
    changed = False
    for (u, _c) in ack_keys:
        if u in h and not h[u].get("b_synced"):
            h[u]["b_synced"] = True
            _stamp_accountability(h[u], u, "bookmark_ack", "chrome_extension_ack")
            _record_accountability_event("bookmark_ack", u, h[u], source="chrome_extension_ack")
            changed = True
    if changed:
        save_history(h)
    return {"status": "ok", "acked": len(ack_keys), "remaining": len(remaining)}

@app.post("/api/v1/history/enrich")
def enrich_hi(background_tasks: BackgroundTasks):
    if enrich_status["running"]:
        return {"status": "already_running"}
    background_tasks.add_task(run_enrich)
    return {"status": "started"}

@app.get("/api/v1/history/enrich/status")
def enrich_hi_status(): return enrich_status

@app.get("/api/v1/history/duplicates")
def get_dupes():
    """Returns groups of history entries that share the same normalized title.
    Useful for spotting DOI vs. direct URL duplicates.
    """
    import re
    h = load_history()
    norm = {}
    for url, item in h.items():
        key = re.sub(r'\s+', ' ', (item.get('title') or '').lower().strip())
        if not key or key in {'untitled', 'untitled pdf'}: continue
        norm.setdefault(key, []).append({'url': url, 'title': item.get('title'), 'category': item.get('category'), 'date': item.get('date')})
    dupes = {k: v for k, v in norm.items() if len(v) > 1}
    return {'total_groups': len(dupes), 'groups': dupes}

@app.get("/api/v1/system_paths")
def get_pa(): return {"output_dir": os.path.abspath(OUTPUT_DIR()), "export_dir": os.path.abspath(EXPORT_DIR())}

@app.post("/api/v1/export/{format}")
def export_db(format: str):
    os.makedirs(EXPORT_DIR(), exist_ok=True)
    h = load_history()
    out = os.path.join(EXPORT_DIR(), f"panop_database_{int(time.time())}")
    
    if format == "json":
        with open(out+".json", "w") as f: json.dump(h, f, indent=4)
    elif format == "csv":
        with open(out+".csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["URL", "Title", "Category", "Date", "AI Learned"])
            for url, data in h.items():
                w.writerow([url, data.get("title",""), data.get("category",""), data.get("date",""), data.get("ai_learned",False)])
    elif format == "md":
        with open(out+".md", "w", encoding="utf-8") as f:
            f.write("# Panop Database Export\n\n")
            for url, data in h.items():
                f.write(f"- **{data.get('category','')}**: [{data.get('title','')}]({url}) ({data.get('date','')})\n")
    elif format == "zip":
        with zipfile.ZipFile(out+".zip", 'w', zipfile.ZIP_DEFLATED) as zf:
            for root, _, files in os.walk(OUTPUT_DIR()):
                if "exports" in root: continue
                for file in files:
                    fp = os.path.join(root, file)
                    zf.write(fp, os.path.relpath(fp, OUTPUT_DIR()))
    return {"status": "ok", "path": out+"."+format}

# ── Chrome-extension content-harvest sink ──────────────────────────────────
# Restored 2026-05-27: Antigravity's parallel pruning removed the harvest
# endpoints that Egon's adapters (lib/adapters/kindle.py, instapaper.py,
# paperpile.py, plus the Media/Reference pages) actively depend on, and
# that the v1.7.4 Chrome extension POSTs to. Same behavior as the prior
# version, including the keep-previous safety from 2026-05-23: an empty
# or failed harvest NEVER clobbers a good library — it only updates the
# debug + timestamp so we can see why the harvest came back empty.
from fastapi import Request as _HReq  # local alias to avoid touching line 24

_KINDLE_LIB_STATE     = os.path.join(OUTPUT_DIR(), "kindle_library_state.json")
_PAPERPILE_LIB_STATE  = os.path.join(OUTPUT_DIR(), "paperpile_library_state.json")
_INSTAPAPER_LIB_STATE = os.path.join(OUTPUT_DIR(), "instapaper_library_state.json")
_YOUTUBE_HISTORY_STATE = os.path.join(OUTPUT_DIR(), "youtube_history_state.json")
_TVTIME_LIB_STATE     = os.path.join(OUTPUT_DIR(), "tvtime_library_state.json")
_TVTIME_EPISODES_STATE = os.path.join(OUTPUT_DIR(), "tvtime_episodes_state.json")


def _harvest_key(it: dict) -> str:
    return str(it.get("url") or it.get("id") or it.get("asin")
               or it.get("title") or "")


def _store_harvest(path: str, body: dict) -> dict:
    """MERGE-by-key, never replace. 2026-06-12 (Bruno: "I want all my data"):
    a partial harvest used to overwrite the whole library — kindle dropped
    31 items -> 6 when a quick page visit caught only the first shelf, and
    YouTube watch history could never accumulate past one page of scrolling.
    Now incoming items upsert into the existing set (fresh fields win per
    item) and items the page didn't show this time are KEPT. Additive, like
    everything else in Egon; a deliberate reset = delete the state file."""
    body["received_at"] = datetime.now().isoformat(timespec="seconds")
    incoming = body.get("items") or []
    merged: dict[str, dict] = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                prev = json.load(f)
            for it in (prev.get("items") or []):
                k = _harvest_key(it)
                if k:
                    merged[k] = it
        except Exception:
            pass
    prev_n = len(merged)
    new_n = 0
    for it in incoming:
        k = _harvest_key(it)
        if not k:
            continue
        if k not in merged:
            new_n += 1
        merged[k] = {**merged.get(k, {}), **it}
    body["items"] = list(merged.values())
    body["count"] = len(body["items"])
    body["merged"] = {"previous": prev_n, "incoming": len(incoming),
                      "new": new_n}
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(body, f, ensure_ascii=False, indent=2)
    return {"status": "ok", "count": body["count"],
            "merged": body["merged"]}


def _read_harvest(path: str) -> dict:
    if not os.path.exists(path):
        return {"status": "no_data", "items": [], "count": 0}
    try:
        with open(path, encoding="utf-8") as f:
            return {"status": "ok", **json.load(f)}
    except Exception as e:
        return {"status": "error", "error": str(e)[:200]}


def _register_harvest_pair(path_const: str, route: str) -> None:
    """Register a (POST upsert / GET read) endpoint pair against `route`,
    backed by the file at `path_const`. DRY because there's one per source."""
    async def _post(req: _HReq):
        try:
            body = await req.json()
            if not isinstance(body, dict) or "items" not in body:
                return {"status": "error", "error": "bad payload"}
            return _store_harvest(path_const, body)
        except Exception as e:
            return {"status": "error", "error": str(e)[:200]}

    def _get():
        return _read_harvest(path_const)

    app.post(route)(_post)
    app.get(route)(_get)


_register_harvest_pair(_KINDLE_LIB_STATE,      "/api/v1/kindle/library")
_register_harvest_pair(_PAPERPILE_LIB_STATE,   "/api/v1/paperpile/library")
_register_harvest_pair(_INSTAPAPER_LIB_STATE,  "/api/v1/instapaper/library")
_register_harvest_pair(_YOUTUBE_HISTORY_STATE, "/api/v1/youtube/history")
_register_harvest_pair(_TVTIME_LIB_STATE,      "/api/v1/tvtime/library")
_register_harvest_pair(_TVTIME_EPISODES_STATE, "/api/v1/tvtime/episodes")


# ── MASTER classifier endpoint — the single brain for ALL surfaces ───────────
# Bruno 2026-06-15: Inbox/Panop, Navigation/Routster, and any future link-
# classifying process must call ONE engine (egon's lib/classifier) so a link is
# never categorised two different ways on two surfaces. JS surfaces (Routster)
# POST here instead of running their own classifier. Optionally fetches the page
# for the content/embedding layers when `fetch:true`.
@app.post("/api/v1/classify")
async def classify_link(req: _HReq):
    try:
        body = await req.json()
    except Exception:
        return {"status": "error", "error": "bad json"}
    url = (body.get("url") or "").strip()
    if not url:
        return {"status": "error", "error": "url required"}
    page_meta = {"title": body.get("title") or "", "abstract": body.get("abstract") or "",
                 "text": body.get("text") or ""}
    if body.get("fetch") and not page_meta["text"]:
        try:
            m = fetch_page_content(_resolve_terminal_tab_url(url))
            if m:
                page_meta = {**m, **{k: v for k, v in page_meta.items() if v}}
        except Exception:
            pass
    try:
        import lib.classifier as _clf
        r = _clf.classify(url, page_meta)
        return {"status": "ok", "url": url, "category": r.category,
                "action": r.action, "confidence": round(r.confidence, 3),
                "layer": r.layer, "evidence": r.evidence}
    except Exception as e:
        return {"status": "error", "error": str(e)[:200]}


# ── Unified-mind endpoints — see external/panop_server/mind_endpoints.py.
# Module-level side effect: importing it registers /api/v1/mind/* routes
# on `app` and initializes state/mind.db. Per the additive pattern from
# the 2026-05-27 reconcile rule — keep main.py lean, add to its tree.
from external.panop_server import mind_endpoints  # noqa: F401,E402


if __name__ == "__main__":
    multiprocessing.freeze_support()
    uvicorn.run("external.panop_server.main:app", host="127.0.0.1", port=8000)
