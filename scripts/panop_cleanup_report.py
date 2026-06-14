"""Generate an HTML review report of everything excluded in the 2026-06-14
Panop Zotero cleanup, so Bruno can revisit links (esp. non-fit exclusions like
author-profile pages) before they're purged from Zotero Trash. Pure read of the
backup + trash records; mutates nothing.
"""
from __future__ import annotations
import json, html
from pathlib import Path
from urllib.parse import urlparse
from datetime import datetime

ROOT = Path(__file__).resolve().parents[1]
BK = ROOT / "state" / "panop" / "backups"
OUT = ROOT / "state" / "panop" / "reports" / "cleanup_2026-06-14_excluded.html"

backup = {it["key"]: it for it in json.loads((BK / "zotero_panop_backup_20260614T012253.json").read_text(encoding="utf-8"))}
trashed = json.loads((BK / "zotero_panop_trashed_20260614T013540.json").read_text(encoding="utf-8"))
suspects = json.loads((BK / "suspects.json").read_text(encoding="utf-8"))
v1 = {k: val for k, val in json.loads((BK / "verdicts.json").read_text(encoding="utf-8")).items() if not k.startswith("_")}
v2 = json.loads((BK / "verdicts2.json").read_text(encoding="utf-8"))
verdicts = {**v1, **v2}


def reject_reason(url, title):
    u = (url or "").lower(); h = urlparse(u).netloc.lower()
    if "/profiles/" in u or "philpeople.org/profiles" in u: return "author / profile page"
    if "bbc.com" in h or "lemonde" in u or "propublica" in u or "bloomberg" in u or "buzzfeed" in u or "city-journal" in u or "thetimes" in u: return "general-news outlet (non-science)"
    if u.endswith((".png", ".jpg", ".jpeg", ".gif")): return "image file"
    if "/tag/" in u or "/tags/" in u or "/archive" in u or "/search" in u or u.rstrip("/").count("/") <= 2: return "homepage / index / tag / search page"
    if "icr.org" in h: return "creationist / non-scholarly source"
    if "futurepedia" in u or "kiwify" in u or "pay." in h or "/newsletter" in u or "essay-competition" in u or "/editais" in u: return "ad / payment / signup / call page"
    if "example.com/egon" in u: return "Egon internal test item"
    return "non-fit for the KMS"


# 1) non-fit exclusions (rejects)
rejects = []
for idx, cat in verdicts.items():
    if cat == "reject":
        it = suspects[int(idx)]
        rejects.append((it.get("title") or "(no title)", it.get("url") or "", reject_reason(it.get("url"), it.get("title"))))

# 2) dupes / dead from the trash log
dupes, dead = [], []
for key, why in trashed.items():
    d = (backup.get(key) or {}).get("data", {})
    row = (d.get("title") or "(no title)", d.get("url") or "", why)
    if why.startswith("dupe_of"):
        kept = why.split(":", 1)[1]
        kd = (backup.get(kept) or {}).get("data", {})
        dupes.append((row[0], row[1], (kd.get("url") or "")))
    else:
        dead.append(row)


def esc(s): return html.escape(str(s or ""))


def rows(items, kind):
    out = []
    for r in items:
        title, url = esc(r[0]), esc(r[1])
        extra = esc(r[2])
        out.append(f'<tr><td>{title}</td><td><a href="{url}" target="_blank">{url[:80]}</a></td><td class="x">{extra}</td></tr>')
    return "\n".join(out)


doc = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Panop cleanup — excluded items (2026-06-14)</title>
<style>
 body{{font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#0f1115;color:#e6e6e6}}
 header{{padding:24px 28px;background:#161a21;border-bottom:1px solid #2a2f3a}}
 h1{{margin:0 0 6px;font-size:20px}} .sub{{color:#9aa4b2}}
 .wrap{{padding:20px 28px;max-width:1200px}}
 h2{{margin:28px 0 8px;font-size:16px}} .count{{color:#7cc6ff}}
 table{{border-collapse:collapse;width:100%;margin:8px 0 18px;background:#11151c}}
 td,th{{text-align:left;padding:7px 10px;border-bottom:1px solid #222833;vertical-align:top}}
 th{{color:#9aa4b2;font-weight:600;position:sticky;top:0;background:#161a21}}
 td.x{{color:#f0b85a;white-space:nowrap}} a{{color:#7cc6ff;text-decoration:none}} a:hover{{text-decoration:underline}}
 .note{{background:#13251a;border:1px solid #235;padding:12px 14px;border-radius:8px;margin:14px 0;color:#bfe6cf}}
 details{{margin:6px 0}} summary{{cursor:pointer;color:#9aa4b2}}
</style></head><body>
<header>
 <h1>Panop Zotero cleanup — excluded items</h1>
 <div class="sub">Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} · everything below is recoverable from Zotero&nbsp;&gt;&nbsp;Trash and from the on-disk backup at state/panop/backups/</div>
</header>
<div class="wrap">
 <div class="note">Nothing here is permanently deleted. <b>Review the non-fit exclusions first</b> — a few may be links you still want (e.g. author profiles). 468 real articles that merely had broken titles were <b>kept</b> (pending re-title), and 137 longform pieces were <b>moved to Instapaper</b>, not deleted.</div>

 <h2>① Non-fit exclusions — <span class="count">{len(rejects)}</span> (review these)</h2>
 <table><tr><th>Title</th><th>URL</th><th>Why excluded</th></tr>
 {rows(rejects, 'reject')}
 </table>

 <details><summary>② Duplicates removed — {len(dupes)} (one copy of each was kept; the kept URL is shown)</summary>
 <table><tr><th>Title</th><th>Removed URL</th><th>Kept copy</th></tr>
 {rows(dupes, 'dupe')}
 </table></details>

 <details><summary>③ Dead links — {len(dead)}</summary>
 <table><tr><th>Title</th><th>URL</th><th>Reason</th></tr>
 {rows(dead, 'dead')}
 </table></details>
</div></body></html>"""

OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(doc, encoding="utf-8")
print(f"non-fit exclusions: {len(rejects)} | duplicates: {len(dupes)} | dead: {len(dead)}")
print(f"report -> {OUT}")
