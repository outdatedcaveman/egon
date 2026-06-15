"""READ-ONLY scan: flag items still sitting in the Zotero Panop collections that
don't fit their category (jobs, shopping, social/forums, events, forms,
profiles, homepages, code repos, etc.). Produces an HTML review report only —
NO mutation. Bruno reviews and approves before anything is trashed.
"""
from __future__ import annotations
import json, re, html
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse
import requests

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "state" / "panop" / "reports" / "zotero_nonfit_review_2026-06-15.html"
COLS = {"Articles": "GKSJSJMJ", "Books": "B3XGDC4J", "Science News": "BRZ3UUIR"}

NEVER_HOSTS = {"reddit.com", "twitter.com", "x.com", "facebook.com", "instagram.com",
    "linkedin.com", "tiktok.com", "youtube.com", "youtu.be", "quora.com", "pinterest.com",
    "lpsg.com", "t.me", "threads.net", "github.com", "gitlab.com", "medium.com"}
JOB_HOSTS = ("gupy.io", "greenhouse.io", "lever.co", "inhire.app", "vagas.com", "solides",
    "successfactors", "myworkdayjobs", "workday", "jobgether", "careers-page.com", "mercor.com",
    "alignerr.com/jobs", "indeed.com", "glassdoor", ".gupy.", "selecao.net", "aceventures")
SHOP_HOSTS = ("mercadolivre", "shopee", "aliexpress", "westwing", "magazineluiza", "amazon.",
    "cubequadros", "moveisalvin", "drogaraia", "tudogostoso", "madamu.com", "saratmoda")
EVENT_HOSTS = ("sympla", "eventbrite", "ingresse", "bileto", "byinti", "superbet")
FORM_HOSTS = ("docs.google.com/forms", "airtable.com", "typeform", "docs.google.com/document")
SERVICE_HOSTS = ("alphasights", "glginsights", "dialectica.io", "wegreened", "upwork.com",
    "contra.com", "coursera.org", "kiwify", "futurepedia")


def host(u):
    h = (urlparse(u or "").netloc or "").lower()
    return h[4:] if h.startswith("www.") else h


def nonfit_reason(url, collection):
    u = (url or "").lower(); h = host(url)
    if h in NEVER_HOSTS and not (collection == "Books" and "github" in h):
        return f"social/forum/code ({h})"
    if any(j in u for j in JOB_HOSTS):
        return "job posting / application"
    if collection != "Books" and any(s in u for s in SHOP_HOSTS):
        return "shopping / product page"
    if any(e in u for e in EVENT_HOSTS):
        return "event / ticket page"
    if any(f in u for f in FORM_HOSTS):
        return "form / google-doc"
    if any(s in u for s in SERVICE_HOSTS):
        return "service / course / signup landing"
    if "philpeople.org/profiles" in u or "/profile/" in u or "/~" in u or re.search(r"/users/[^/]+/?$", u):
        return "author / profile / faculty page"
    p = urlparse(u)
    if not (p.path or "").strip("/") and not p.query:
        return "homepage / index (no article path)"
    return None


def main():
    pe = json.loads((ROOT / "panop_env.json").read_text(encoding="utf-8-sig"))
    H = {"Zotero-API-Key": pe["zotero_api_key"], "Zotero-API-Version": "3"}
    base = f"https://api.zotero.org/users/{pe['zotero_user_id']}"
    flagged = {c: [] for c in COLS}
    counts = {}
    for cname, ckey in COLS.items():
        start = 0; n = 0
        while True:
            r = requests.get(f"{base}/collections/{ckey}/items/top?limit=100&start={start}", headers=H, timeout=40)
            if r.status_code != 200: break
            b = r.json()
            if not b: break
            for it in b:
                d = it.get("data", {}); n += 1
                reason = nonfit_reason(d.get("url", ""), cname)
                if reason:
                    flagged[cname].append((d.get("title", ""), d.get("url", ""), reason, it.get("key")))
            if len(b) < 100: break
            start += len(b)
        counts[cname] = n

    def esc(s): return html.escape(str(s or ""))
    def rows(items):
        return "\n".join(
            f'<tr><td>{esc(t)}</td><td><a href="{esc(u)}" target="_blank">{esc(u[:75])}</a></td>'
            f'<td class="x">{esc(why)}</td></tr>' for t, u, why, k in items)

    total = sum(len(v) for v in flagged.values())
    secs = "\n".join(
        f'<h2>{c} — <span class="count">{len(flagged[c])}</span> suspected non-fits '
        f'(of {counts.get(c,0)})</h2><table><tr><th>Title</th><th>URL</th><th>Why flagged</th></tr>{rows(flagged[c])}</table>'
        for c in COLS)
    doc = f"""<!doctype html><html><head><meta charset="utf-8"><title>Zotero non-fit review</title>
<style>body{{font:14px/1.5 -apple-system,Segoe UI,sans-serif;margin:0;background:#0f1115;color:#e6e6e6}}
header{{padding:22px 28px;background:#161a21;border-bottom:1px solid #2a2f3a}}h1{{margin:0;font-size:19px}}
.wrap{{padding:18px 28px;max-width:1200px}}h2{{font-size:15px;margin:24px 0 6px}}.count{{color:#f0b85a}}
table{{border-collapse:collapse;width:100%;margin:6px 0 16px;background:#11151c}}td,th{{text-align:left;padding:6px 10px;border-bottom:1px solid #222833;vertical-align:top}}
th{{color:#9aa4b2}}td.x{{color:#f0b85a;white-space:nowrap}}a{{color:#7cc6ff;text-decoration:none}}
.note{{background:#1a1420;border:1px solid #533;padding:11px 14px;border-radius:8px;margin:12px 0;color:#e6c9d4}}</style></head>
<body><header><h1>Zotero Panop — suspected non-fits for review ({total})</h1>
<div class="sub" style="color:#9aa4b2">Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} · READ-ONLY — nothing trashed. Approve and I'll trash (reversibly) the ones you confirm.</div></header>
<div class="wrap"><div class="note">High-precision rules only (jobs/shopping/social/events/forms/profiles/homepages). Real articles with unusual hosts are NOT flagged. Review, then tell me which to remove.</div>
{secs}</div></body></html>"""
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(doc, encoding="utf-8")
    import shutil, os
    dl = os.path.join(os.environ["USERPROFILE"], "Desktop", "Panop_nonfit_review_2026-06-15.html")
    shutil.copy(OUT, dl)
    print(f"scanned: {counts}")
    print(f"flagged non-fits: {dict((c,len(v)) for c,v in flagged.items())} total={total}")
    print(f"report -> {dl}")


if __name__ == "__main__":
    main()
