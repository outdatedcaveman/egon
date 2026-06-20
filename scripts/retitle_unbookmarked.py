"""Recover clean titles for history items that failed to bookmark because their
captured title is junk (bot-challenge page, "Untitled", bare hostname). We look
the real title up via metadata APIs (Crossref for DOIs, arXiv API for arXiv IDs)
— NOT by re-fetching the walled publisher page — then PATCH it through the
server's /api/v1/history/edit so there's no file race. After this, a bookmark
sync writes them (good title passes the quality gate) and they become closeable.

  python scripts/retitle_unbookmarked.py          # dry-run
  python scripts/retitle_unbookmarked.py --commit
"""
from __future__ import annotations
import os, sys, json, re, time, argparse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

ROOT = Path(__file__).resolve().parents[1]
HIST = ROOT / "state" / "panop" / "panop_history.json"
API = "http://127.0.0.1:8000"
# Contact for the Crossref/arXiv "polite pool" — overridable via env so no
# personal address is committed to the public repo.
MAILTO = os.environ.get("EGON_CONTACT_EMAIL", "kms@egon.local")
UA = {"User-Agent": f"EgonKMS/1.0 (mailto:{MAILTO})"}
_DOI = re.compile(r"\b(10\.\d{4,9}/[^\s?#&\"']+)", re.I)
_ARXIV = re.compile(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})", re.I)

_JUNK = ("just a moment", "untitled", "access denied", "page not found",
         "403 forbidden", "404", "checking your", "are you a robot", "captcha",
         "loading", "redirecting", "not found", "attention required",
         "one moment", "please wait", "site maintenance", "error")

def is_junk(title: str, url: str = "") -> bool:
    t = (title or "").strip().lower()
    if not t or len(t) < 6:
        return True
    if any(j in t for j in _JUNK):
        return True
    # bare hostname as title (e.g. "www.biorxiv.org", "arxiv.org", "doi.org")
    host = re.sub(r"^https?://", "", url).split("/")[0].lower()
    if t == host or t == host.replace("www.", "") or t.count(" ") == 0 and "." in t:
        return True
    return False

def title_from_crossref(doi: str):
    try:
        r = requests.get(f"https://api.crossref.org/works/{doi}", headers=UA, timeout=12)
        if r.status_code == 200:
            t = (((r.json() or {}).get("message") or {}).get("title") or [None])[0]
            return t.strip() if t else None
    except Exception:
        return None
    return None

def title_from_arxiv(aid: str):
    try:
        r = requests.get(f"http://export.arxiv.org/api/query?id_list={aid}", headers=UA, timeout=12)
        if r.status_code == 200:
            m = re.search(r"<entry>.*?<title>(.*?)</title>", r.text, re.S)
            if m:
                return re.sub(r"\s+", " ", m.group(1)).strip()
    except Exception:
        return None
    return None

def clean_title_for(url: str, item: dict):
    doi = (item.get("doi") or "").strip()
    if not doi:
        m = _DOI.search(url or "")
        if m:
            doi = m.group(1).rstrip(".").rstrip(")")
    a = _ARXIV.search(url or "")
    if a:
        t = title_from_arxiv(a.group(1))
        if t and not is_junk(t):
            return t
    if doi:
        t = title_from_crossref(doi)
        if t and not is_junk(t):
            return t
    return None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    h = json.loads(HIST.read_text(encoding="utf-8"))
    targets = [(u, it) for u, it in h.items()
               if isinstance(it, dict) and not it.get("b_synced") and is_junk(it.get("title"), u)]
    print(f"{len(targets)} unbookmarked junk-title items to recover")

    def work(args_):
        u, it = args_
        t = clean_title_for(u, it)
        return (u, it, t)

    recovered, failed = [], []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for u, it, t in pool.map(work, targets):
            if t:
                recovered.append((u, it, t))
            else:
                failed.append((u, it))

    print(f"recovered {len(recovered)} titles via metadata; {len(failed)} unrecoverable (no DOI/arXiv id)")
    for u, it, t in recovered[:12]:
        print(f"  + {t[:65]!r}  <- {u[:55]}")

    if not args.commit:
        print("\n(dry-run; pass --commit to PATCH titles through the server)")
        return

    ok = 0
    for u, it, t in recovered:
        try:
            payload = {"old_url": u, "url": u, "title": t,
                       "category_id": it.get("cat_id") or "", "date": it.get("date") or ""}
            r = requests.post(f"{API}/api/v1/history/edit", json=payload, timeout=15)
            if r.status_code == 200:
                ok += 1
        except Exception:
            pass
    print(f"committed {ok}/{len(recovered)} title updates")

if __name__ == "__main__":
    main()
