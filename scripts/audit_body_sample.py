"""READ-ONLY audit. Sample the suspect Zotero buckets, re-judge each item by its
BODY (lib/body_classify), and render a before->after HTML report — recovered
title, new verdict, evidence source. Mutates NOTHING. Bruno reviews, then we run
the full pass only on what he approves.

  python scripts/audit_body_sample.py --n 150
"""
from __future__ import annotations
import sys, json, time, html, argparse, random
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import requests
from lib.body_classify import classify_by_body

# Panop subcollections -> the category they currently claim to be.
COLLS = {"GKSJSJMJ": "articles", "B3XGDC4J": "books", "BRZ3UUIR": "science_news"}

# Cheap "this title looks wrong" heuristic — used only to BIAS the sample toward
# suspect items so the report is informative. Not a verdict.
_SUSPECT = ("just a moment", "are you a robot", "access denied", "preference",
            "unsubscribe", "sign in", "log in", "session has timed out", "404",
            "page not found", "whois", "checking your", "redirecting", "loading")


def looks_suspect(title, url):
    t = (title or "").lower()
    if any(s in t for s in _SUSPECT):
        return True
    if len(t) < 6:
        return True
    host = url.split("/")[2].lower() if "://" in url else ""
    if any(h in host for h in ("youtube.", "x.com", "twitter.", "facebook.", "reddit.",
                               "web3.", "watermark", "link.", "list-manage", "email")):
        return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=150)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    random.seed(a.seed)

    pe = json.loads((ROOT / "panop_env.json").read_text(encoding="utf-8-sig"))
    H = {"Zotero-API-Key": pe["zotero_api_key"], "Zotero-API-Version": "3"}
    base = f"https://api.zotero.org/users/{pe['zotero_user_id']}"

    pool = []   # (current_cat, key, title, url)
    for ck, cur in COLLS.items():
        start = 0
        while True:
            r = requests.get(f"{base}/collections/{ck}/items/top?limit=100&start={start}",
                             headers=H, timeout=40)
            if r.status_code != 200 or not r.json():
                break
            b = r.json()
            for it in b:
                d = it.get("data", {})
                u = d.get("url") or ""
                if u.startswith("http"):
                    pool.append((cur, it["key"], d.get("title", ""), u))
            if len(b) < 100:
                break
            start += len(b)
    print(f"library pool: {len(pool)} items across {list(COLLS.values())}")

    suspect = [p for p in pool if looks_suspect(p[2], p[3])]
    clean = [p for p in pool if not looks_suspect(p[2], p[3])]
    random.shuffle(suspect); random.shuffle(clean)
    # Bias 2:1 toward suspect so the report shows the failures, but include clean
    # controls to confirm we don't wreck good items.
    n_sus = min(len(suspect), int(a.n * 0.66))
    sample = suspect[:n_sus] + clean[:a.n - n_sus]
    random.shuffle(sample)
    print(f"sampling {len(sample)} ({n_sus} suspect + {len(sample)-n_sus} controls)")

    rows = []
    for i, (cur, key, title, url) in enumerate(sample, 1):
        try:
            v = classify_by_body(url)
        except Exception as e:
            v = {"category": None, "source": f"error:{type(e).__name__}", "title": ""}
        new = v.get("category")
        verdict = ("KEEP" if new == cur else
                   "REJECT" if new == "reject" else
                   "MOVE" if new else
                   "UNSURE")
        rows.append({"cur": cur, "new": new, "verdict": verdict, "src": v.get("source", ""),
                     "old_title": title, "new_title": v.get("title", ""), "url": url})
        if i % 10 == 0:
            print(f"  {i}/{len(sample)}")
        time.sleep(1.2)

    # ── summary + HTML ────────────────────────────────────────────────────
    from collections import Counter
    vc = Counter(r["verdict"] for r in rows)
    order = {"REJECT": 0, "MOVE": 1, "UNSURE": 2, "KEEP": 3}
    rows.sort(key=lambda r: (order.get(r["verdict"], 9), r["cur"]))

    badge = {"KEEP": "#1a7f37", "MOVE": "#9a6700", "REJECT": "#cf222e", "UNSURE": "#6e7781"}
    trs = []
    for r in rows:
        nt = r["new_title"] or "<i>—</i>"
        retitled = (r["new_title"] and r["new_title"].lower() != (r["old_title"] or "").lower())
        trs.append(
            f"<tr><td><b style='color:{badge[r['verdict']]}'>{r['verdict']}</b></td>"
            f"<td>{html.escape(r['cur'])}</td><td>{html.escape(str(r['new']))}</td>"
            f"<td><code>{html.escape(r['src'])}</code></td>"
            f"<td>{html.escape(r['old_title'][:80])}</td>"
            f"<td>{'🔁 ' if retitled else ''}{html.escape(str(nt)[:80])}</td>"
            f"<td><a href='{html.escape(r['url'])}' style='color:#0969da'>{html.escape(r['url'][:70])}</a></td></tr>")

    summary = " · ".join(f"<b style='color:{badge[k]}'>{k}</b> {vc.get(k,0)}"
                         for k in ("KEEP", "MOVE", "REJECT", "UNSURE"))
    out = ROOT / "state" / "panop" / "body_audit_report.html"
    out.write_text(f"""<!doctype html><meta charset=utf-8>
<title>Body-first classifier — audit sample</title>
<style>body{{font:14px/1.5 system-ui;margin:24px;color:#1f2328;max-width:1400px}}
table{{border-collapse:collapse;width:100%}}th,td{{padding:5px 9px;border-bottom:1px solid #eaecef;text-align:left;vertical-align:top}}
th{{background:#f6f8fa;position:sticky;top:0}}code{{font-size:12px;color:#57606a}}tr:hover{{background:#f6f8fa}}</style>
<h2>Body-first re-classification — audit sample ({len(rows)} items, read-only)</h2>
<p style='font-size:16px'>{summary}</p>
<p style='color:#57606a'>Each item re-judged from its <b>page body</b> (citation-meta / product / book schema / contentless = reject), URL only as fallback when walled. <b>KEEP</b>=body agrees with current bucket · <b>MOVE</b>=belongs elsewhere · <b>REJECT</b>=not saveable · <b>UNSURE</b>=walled+ambiguous (would go to AI). 🔁 = real title recovered.</p>
<table><tr><th>verdict</th><th>now in</th><th>body says</th><th>evidence</th><th>current title</th><th>recovered title</th><th>url</th></tr>
{''.join(trs)}</table>""", encoding="utf-8")
    print(f"\nSUMMARY: {dict(vc)}")
    print(f"report: {out}")


if __name__ == "__main__":
    main()
