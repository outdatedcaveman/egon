"""AI-pass over the 464 walled/ambiguous items (Bruno asked: sanity-check then
save). The crude None->content_longform default was wrong: host clustering shows
most 'blocked' items are WALLED JOURNAL/PREPRINT pages (articles), not longform.
Apply judgement by host cluster + academic-URL patterns; drop tracking/search
junk. Updates rescue_history_classified.json in place (backup first).
"""
from __future__ import annotations
import sys, json, shutil
from pathlib import Path
from urllib.parse import urlparse
from datetime import datetime

ROOT = Path(__file__).resolve().parents[1]
ST = ROOT / "state" / "panop"

# Walled scholarly hosts (journals/preprints/archives) -> articles.
JOURNAL = {
    "philarchive.org", "cell.com", "journals.sagepub.com", "worldscientific.com",
    "journals.uchicago.edu", "biorxiv.org", "siam.org", "epubs.siam.org", "jneurosci.org",
    "direct.mit.edu", "pubsonline.informs.org", "academia.edu", "quod.lib.umich.edu",
    "journals.asm.org", "pubs.acs.org", "wspc.scienceconnect.io", "pnas.scienceconnect.io",
    "wiley.scienceconnect.io", "authorea.com", "thelancet.com", "jamanetwork.com",
    "guilfordjournals.com", "cacm.acm.org", "link.aps.org", "nejm.org", "jov.arvojournals.org",
    "publications.aaahq.org", "escholarship.org", "openreview.net", "ui.adsabs.harvard.edu",
}
LONGFORM = {"substack.com", "economist.com", "briancalbrecht.com", "noahpinion.blog"}
BOOKS = {"global.oup.com", "bookdna.com"}
JUNK = {"sg-links.stackoverflow.email", "analytics.twitter.com", "search.censys.io",
        "google.com", "instagram.com", "accounts.google.com"}


def host_cat(host, cur):
    h = host.replace("www.", "")
    if h in JUNK:
        return "reject"
    if h in JOURNAL:
        return "articles"
    if h in BOOKS:
        return "books"
    if h in LONGFORM or h.endswith(".substack.com"):
        return "content_longform"
    # academic-URL patterns the host map didn't name explicitly
    if (h.endswith(".edu") or "journals." in h or "pubs." in h or h.startswith("journal")
            or ".scienceconnect.io" in h or "arxiv" in h):
        return "articles"
    return cur  # leave as content_longform (blog-like default)


def main():
    f = ST / "rescue_history_classified.json"
    d = json.loads(f.read_text(encoding="utf-8"))
    shutil.copy(f, f.with_suffix(f".{datetime.now():%Y%m%dT%H%M%S}.bak"))

    from collections import Counter
    changed = Counter()
    for u, v in list(d.items()):
        # only touch the crude-default uncertain ones; keep confident verdicts
        if v["category"] == "content_longform" and (v.get("src") or "").startswith(("blocked", "needs_ai")):
            nc = host_cat(urlparse(u).netloc, v["category"])
            if nc != v["category"]:
                changed[f"-> {nc}"] += 1
            if nc == "reject":
                del d[u]
            else:
                v["category"] = nc
    print("relabelled:", dict(changed))
    print("final categories:", dict(Counter(v["category"] for v in d.values())), "| total", len(d))
    f.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
