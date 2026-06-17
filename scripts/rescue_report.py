"""Regenerate a clean, accurate review report from the FINAL rescue state
(after pass-2). Two tables Bruno actually wants to eyeball:
  - RESTORE set (2850) — "am I bringing back any junk?" — grouped, with evidence.
  - final DISCARD set (183) — "am I leaving anything real out?" — every item.
READ-ONLY. Writes state/panop/rescue_review.html.
"""
from __future__ import annotations
import sys, json, html
from pathlib import Path
from collections import Counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.rescue_audit import load_zotero_trash, canon, rescue_verdict, host_of


def rows_table(rows, cols):
    trs = []
    for r in rows:
        tds = "".join(f"<td>{c}</td>" for c in cols(r))
        trs.append(f"<tr>{tds}</tr>")
    return "\n".join(trs)


def main():
    pe = json.loads((ROOT / "panop_env.json").read_text(encoding="utf-8-sig"))
    cands = json.loads((ROOT / "state" / "panop" / "rescue_restore_candidates.json").read_text(encoding="utf-8"))
    restore_canon = {canon(c["url"]) for c in cands}

    # re-derive final DISCARD = URL-decided DISCARD not rescued into candidates
    items = load_zotero_trash(pe)
    for fn in ("history_harddelete.json", "history_junk_to_purge.json"):
        p = ROOT / "state" / "panop" / fn
        if p.exists():
            for j in json.loads(p.read_text(encoding="utf-8")):
                items.append({"src": fn, "title": j.get("title", ""), "url": j.get("url", "")})
    seen, discard = set(), []
    for it in items:
        c = canon(it["url"])
        if not c or c in seen:
            continue
        seen.add(c)
        v, src, rt = rescue_verdict(it["title"], it["url"], allow_fetch=False)
        if v == "DISCARD" and c not in restore_canon:
            discard.append({**it, "evidence": src})

    badge = {"RESTORE": "#1a7f37", "RESTORE?": "#9a6700"}
    cands.sort(key=lambda c: (c["src"] != "zotero_trash", c["verdict"] != "RESTORE", c["evidence"]))

    def rcols(r):
        return [f"<b style='color:{badge.get(r['verdict'],'#333')}'>{r['verdict']}</b>",
                "library" if r["src"] == "zotero_trash" else "history",
                f"<code>{html.escape(r['evidence'])}</code>",
                html.escape((r.get("rtitle") or r.get("title") or "")[:75]),
                f"<a href='{html.escape(r['url'])}' style='color:#0969da'>{html.escape(r['url'][:68])}</a>"]

    def dcols(r):
        return [f"<code>{html.escape(r['evidence'])}</code>",
                html.escape((r.get("title") or "")[:75]),
                f"<a href='{html.escape(r['url'])}' style='color:#0969da'>{html.escape(r['url'][:68])}</a>"]

    zt = sum(1 for c in cands if c["src"] == "zotero_trash")
    sumr = dict(Counter(c["verdict"] for c in cands))
    out = ROOT / "state" / "panop" / "rescue_review.html"
    out.write_text(f"""<!doctype html>
<html lang=en>
<head><meta charset="utf-8"><title>Rescue review (final)</title>
<style>
body{{font:14px/1.5 system-ui,Segoe UI,Arial;margin:24px;color:#1f2328;max-width:1500px}}
table{{border-collapse:collapse;width:100%;margin:8px 0 28px}}
th,td{{padding:5px 9px;border-bottom:1px solid #eaecef;text-align:left;vertical-align:top}}
th{{background:#f6f8fa;position:sticky;top:0}}
code{{font-size:12px;color:#57606a}}
tr:hover{{background:#f6f8fa}}
h2{{margin-top:28px}}.k{{color:#1a7f37}}.d{{color:#cf222e}}
</style></head>
<body>
<h1>Rescue review — final state</h1>
<p style="font-size:16px">Re-judged {len(cands)+len(discard)} discarded items by body.
<b class="k">{len(cands)} to RESTORE</b> ({zt} were in your library, {len(cands)-zt} from history) ·
<b class="d">{len(discard)} stay discarded</b> (confirmed junk).</p>
<p style="color:#57606a">Nothing has been changed. To bring the library items back:
<code>python scripts/rescue_restore.py --commit</code> (reversible — re-trashable).
A spreadsheet version sits next to this file: <code>rescue_review.csv</code>.</p>

<h2 class="d">① The {len(discard)} that STAY discarded — confirm these are all junk</h2>
<table>
<thead><tr><th>evidence</th><th>title</th><th>url</th></tr></thead>
<tbody>
{rows_table(discard, dcols)}
</tbody></table>

<h2 class="k">② The {len(cands)} to RESTORE — confirm none are junk (library first)</h2>
<table>
<thead><tr><th>verdict</th><th>from</th><th>evidence</th><th>title</th><th>url</th></tr></thead>
<tbody>
{rows_table(cands, rcols)}
</tbody></table>
</body></html>
""", encoding="utf-8")

    # CSV fallback (opens in Excel; sortable/filterable)
    import csv
    csv_path = ROOT / "state" / "panop" / "rescue_review.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["action", "verdict", "source", "evidence", "title", "url"])
        for c in cands:
            w.writerow(["RESTORE", c["verdict"], "library" if c["src"] == "zotero_trash" else "history",
                        c["evidence"], c.get("rtitle") or c.get("title") or "", c["url"]])
        for d in discard:
            w.writerow(["DISCARD", "DISCARD", "", d["evidence"], d.get("title") or "", d["url"]])

    print(f"restore: {len(cands)} {sumr} | discard: {len(discard)}")
    print(f"review report: {out}")
    print(f"spreadsheet:   {csv_path}")


if __name__ == "__main__":
    main()
