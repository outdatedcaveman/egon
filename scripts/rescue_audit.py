"""TIGHT rescue check over everything that was thrown away. Bruno 2026-06-17:
"make sure we're not throwing anything out." Re-judges every discarded item by
its BODY, biased HARD toward restore — an item is confirmed-discard ONLY if it
positively proves to be non-content (search/auth/mail host, or a contentless
preference/login/unsubscribe/404 page). Anything with real content, any paper
structure, any known-good content host, or ANY doubt -> flagged RESTORE.

Sources of "thrown away":
  - Zotero Trash (items removed from the library; restorable via deleted:0)
  - state/panop/history_harddelete.json   (history URLs marked for deletion)
  - state/panop/history_junk_to_purge.json (history URLs marked junk)

READ-ONLY. Emits an HTML report + state/panop/rescue_restore_candidates.json.
Nothing is restored until Bruno approves.

  python scripts/rescue_audit.py
"""
from __future__ import annotations
import sys, json, time, html
from pathlib import Path
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import requests
from lib.body_classify import classify_by_body, _UTILITY_RE, _PAPER_HOST_PATH

# Genuine non-content surfaces — confirm discard without a fetch.
HARD_REJECT_HOSTS = {
    "accounts.google.com", "mail.google.com", "calendar.google.com", "drive.google.com",
    "docs.google.com", "outlook.live.com", "outlook.office.com", "web.whatsapp.com",
    "messenger.com", "translate.google.com", "login.microsoftonline.com",
}
# Hosts whose content is real by definition — auto-RESTORE without a fetch.
GOOD_CONTENT_HOSTS = (
    "arxiv.org", "philpapers.org", "ncbi.nlm.nih.gov", "pubmed.ncbi.nlm.nih.gov",
    "jstor.org", "nature.com", "science.org", "sciencedirect.com", "springer.com",
    "link.springer.com", "academic.oup.com", "biorxiv.org", "medrxiv.org", "pnas.org",
    "aeaweb.org", "pubs.aeaweb.org", "psycnet.apa.org", "ssrn.com", "papers.ssrn.com",
    "tandfonline.com", "wiley.com", "onlinelibrary.wiley.com", "cambridge.org",
    "plato.stanford.edu", "iep.utm.edu", "phys.org", "techxplore.com", "medicalxpress.com",
    "sciencedaily.com", "quantamagazine.org", "newscientist.com", "scientificamerican.com",
    "nautil.us", "aeon.co", "noahpinion.blog", "astralcodexten.com", "lesswrong.com",
    "marginalrevolution.com", "statnews.com", "nejm.org", "thelancet.com", "cell.com",
    "semanticscholar.org", "openalex.org", "doi.org", "dx.doi.org", "researchgate.net",
    "ieeexplore.ieee.org", "dl.acm.org", "mitpress.mit.edu", "press.princeton.edu",
)
TRACK = {"utm_source","utm_medium","utm_campaign","utm_term","utm_content","fbclid","gclid",
         "mc_cid","mc_eid","igshid","ref","ref_src","yclid","msclkid","spm","share","gad_source"}


def canon(u):
    try:
        p = urlparse(u); net = (p.netloc or "").lower()
        if net.startswith("m."): net = "www." + net[2:]
        path = (p.path or "").rstrip("/") or "/"
        qs = sorted((k, v) for k, v in parse_qsl(p.query) if k.lower() not in TRACK)
        return urlunparse(((p.scheme or "https").lower(), net, path, "", urlencode(qs), ""))
    except Exception:
        return u


def is_search(u):
    ul = u.lower()
    return ("/search?" in ul or "google.com/url?" in ul or "?q=" in ul and "search" in ul
            or "bing.com/search" in ul or "duckduckgo.com/?q" in ul)


def host_of(u):
    return (urlparse(u).netloc or "").lower()


SAVEABLE = {"articles", "books", "science_news", "content_longform", "references",
            "data_tools", "shopping", "study_work", "opportunities", "curios"}


def rescue_verdict(title, url, allow_fetch=True):
    """Return (verdict, evidence, recovered_title). verdict in
    RESTORE / RESTORE? / DISCARD. Biased toward RESTORE."""
    host = host_of(url)
    # 1. genuine non-content -> confirm discard, no fetch
    if host in HARD_REJECT_HOSTS or is_search(url):
        return "DISCARD", "url:noncontent_host", title
    if _UTILITY_RE.search(url):
        return "DISCARD", "url:utility", title
    # 2. paper structure / known-good content host -> RESTORE, no fetch
    if any(host.endswith(h) for h in GOOD_CONTENT_HOSTS):
        return "RESTORE", "url:good_host", title
    for h, p in _PAPER_HOST_PATH:
        if host.endswith(h) and p in (urlparse(url).path.lower()):
            return "RESTORE", "url:paper_path", title
    if not allow_fetch:
        return "RESTORE?", "url:unfetched_default_keep", title
    # 3. uncertain -> judge by BODY
    try:
        v = classify_by_body(url)
    except Exception as e:
        return "RESTORE?", f"error:{type(e).__name__}", title
    cat, src = v.get("category"), v.get("source", "")
    rt = v.get("title") or title
    if cat in SAVEABLE:
        return "RESTORE", src, rt
    if cat == "reject":
        return "DISCARD", src, rt
    # None: real-but-ambiguous (needs_ai) or blocked -> conservative keep
    return "RESTORE?", src, rt


def load_zotero_trash(pe):
    H = {"Zotero-API-Key": pe["zotero_api_key"], "Zotero-API-Version": "3"}
    base = f"https://api.zotero.org/users/{pe['zotero_user_id']}"
    out, start = [], 0
    while True:
        r = requests.get(f"{base}/items/trash?limit=100&start={start}", headers=H, timeout=40)
        if r.status_code != 200 or not r.json():
            break
        b = r.json()
        for it in b:
            d = it.get("data", {})
            u = d.get("url") or ""
            if u.startswith("http"):
                out.append({"src": "zotero_trash", "key": it["key"], "version": it["version"],
                            "title": d.get("title", ""), "url": u})
        if len(b) < 100:
            break
        start += len(b)
    return out


def main():
    pe = json.loads((ROOT / "panop_env.json").read_text(encoding="utf-8-sig"))
    items = load_zotero_trash(pe)
    print(f"Zotero trash loaded: {len(items)}")
    for fn in ("history_harddelete.json", "history_junk_to_purge.json"):
        p = ROOT / "state" / "panop" / fn
        if p.exists():
            for j in json.loads(p.read_text(encoding="utf-8")):
                items.append({"src": fn.replace("history_", "").replace(".json", ""),
                              "key": None, "version": None,
                              "title": j.get("title", ""), "url": j.get("url", "")})
    print(f"total discarded loaded: {len(items)}")

    # dedup by canon url (keep first = prefer Zotero-trash provenance)
    seen, uniq = set(), []
    for it in items:
        c = canon(it["url"])
        if c and c not in seen:
            seen.add(c); uniq.append(it)
    print(f"unique discarded urls: {len(uniq)}")

    # Phase 1: decide everything we can by URL alone — no network (instant).
    rows, need_fetch = [], []
    for it in uniq:
        v, src, rt = rescue_verdict(it["title"], it["url"], allow_fetch=False)
        if src == "url:unfetched_default_keep":
            need_fetch.append(it)            # the uncertain middle -> body-fetch
        else:
            rows.append({**it, "verdict": v, "evidence": src, "rtitle": rt})
    print(f"decided by URL (no fetch): {len(rows)} | need body-fetch: {len(need_fetch)}", flush=True)

    # Phase 2: body-fetch the uncertain middle CONCURRENTLY (network-bound).
    from concurrent.futures import ThreadPoolExecutor, as_completed
    fetches = [0]

    def judge(it):
        v, src, rt = rescue_verdict(it["title"], it["url"], allow_fetch=True)
        return {**it, "verdict": v, "evidence": src, "rtitle": rt}

    done = 0
    with ThreadPoolExecutor(max_workers=12) as ex:
        futs = {ex.submit(judge, it): it for it in need_fetch}
        for f in as_completed(futs):
            try:
                rows.append(f.result())
            except Exception as e:
                it = futs[f]
                rows.append({**it, "verdict": "RESTORE?", "evidence": f"error:{type(e).__name__}",
                             "rtitle": it["title"]})
            done += 1
            fetches[0] = done
            if done % 100 == 0:
                print(f"  fetched {done}/{len(need_fetch)}", flush=True)
    fetches = fetches[0]

    from collections import Counter
    vc = Counter(r["verdict"] for r in rows)
    restore = [r for r in rows if r["verdict"] in ("RESTORE", "RESTORE?")]
    # machine-readable restore list (Zotero-trash items carry key+version)
    (ROOT / "state" / "panop" / "rescue_restore_candidates.json").write_text(
        json.dumps(restore, ensure_ascii=False, indent=1), encoding="utf-8")

    # HTML — RESTORE first, then RESTORE?, then DISCARD
    order = {"RESTORE": 0, "RESTORE?": 1, "DISCARD": 2}
    rows.sort(key=lambda r: (order[r["verdict"]], r["src"]))
    badge = {"RESTORE": "#1a7f37", "RESTORE?": "#9a6700", "DISCARD": "#cf222e"}
    trs = []
    for r in rows:
        rt = html.escape((r["rtitle"] or "")[:80]) or "<i>—</i>"
        trs.append(
            f"<tr><td><b style='color:{badge[r['verdict']]}'>{r['verdict']}</b></td>"
            f"<td>{html.escape(r['src'])}</td><td><code>{html.escape(r['evidence'])}</code></td>"
            f"<td>{html.escape((r['title'] or '')[:70])}</td><td>{rt}</td>"
            f"<td><a href='{html.escape(r['url'])}' style='color:#0969da'>{html.escape(r['url'][:70])}</a></td></tr>")
    summary = " · ".join(f"<b style='color:{badge[k]}'>{k}</b> {vc.get(k,0)}"
                         for k in ("RESTORE", "RESTORE?", "DISCARD"))
    out = ROOT / "state" / "panop" / "rescue_audit_report.html"
    out.write_text(f"""<!doctype html><meta charset=utf-8><title>Rescue check — discarded items</title>
<style>body{{font:14px/1.5 system-ui;margin:24px;color:#1f2328;max-width:1500px}}
table{{border-collapse:collapse;width:100%}}th,td{{padding:5px 9px;border-bottom:1px solid #eaecef;text-align:left;vertical-align:top}}
th{{background:#f6f8fa;position:sticky;top:0}}code{{font-size:12px;color:#57606a}}tr:hover{{background:#f6f8fa}}</style>
<h2>Rescue check — {len(rows)} discarded items re-judged by body (read-only)</h2>
<p style='font-size:16px'>{summary} &nbsp;·&nbsp; body-fetches: {fetches}</p>
<p style='color:#57606a'><b>RESTORE</b> = body/structure proves real content (bring it back) · <b>RESTORE?</b> = walled or ambiguous, kept out of caution (defaults to keep) · <b>DISCARD</b> = body positively proves non-content (search/login/preference/404). Bias is toward keeping — only positively-proven junk is discarded.</p>
<table><tr><th>verdict</th><th>from</th><th>evidence</th><th>discarded title</th><th>recovered title</th><th>url</th></tr>
{''.join(trs)}</table>""", encoding="utf-8")
    print(f"\nSUMMARY: {dict(vc)}  (body-fetches: {fetches})")
    print(f"restore candidates: {len(restore)} -> state/panop/rescue_restore_candidates.json")
    print(f"report: {out}")


if __name__ == "__main__":
    main()
