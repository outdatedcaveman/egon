"""Apply the full-library re-classification (live_reclassify.jsonl) to Zotero:
  - MOVE each item to the collection its body says it belongs to.
  - RETITLE items whose current title is junk ("Just a moment…", bare host,
    empty) when a real title was recovered — never overwrite a good title.
  - TRASH items the body proves are non-content (reject) — reversible.
Uncertain verdicts (None / blocked / needs_ai with no real title) are LEFT as-is
(no churn). Re-fetches live versions+collections so nothing 412s; backs up every
item's original (collections, title) first; resumable via an applied-ledger.

  python scripts/apply_live_library.py            # dry-run plan
  python scripts/apply_live_library.py --commit   # apply (reversible)
"""
from __future__ import annotations
import sys, json, time, argparse
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse
from collections import Counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import requests

ST = ROOT / "state" / "panop"
CKPT = ST / "live_reclassify.jsonl"
APPLIED = ST / "live_reclassify_applied.jsonl"
BK = ST / "backups"

CATKEY = {"articles": "GKSJSJMJ", "books": "B3XGDC4J", "science_news": "BRZ3UUIR",
          "content_longform": "S2IP249A", "references": "2DDCVMKV", "data_tools": "QR7WM9FE",
          "curios": "DSA4TSUE", "opportunities": "SGSRJA3F", "shopping": "WBTQEC5J"}
PANOP_SUBTREE = set(CATKEY.values()) | {"24A43HSI"}
KEY2CAT = {v: k for k, v in CATKEY.items()}

_BADTITLE = ("just a moment", "attention required", "access denied", "page not found",
             "not found", "untitled", "forbidden", "are you a robot", "checking your",
             "request limit", "redirecting", "loading", "site maintenance", "error 4",
             "error 5", "this site can", "bot verification", "cloudflare", "captcha",
             "sign in", "log in", "please wait", "unavailable", "blocked", "denied",
             "403", "404", "500", "502", "503", "robot", "verify you", "security check",
             "one moment", "enable javascript", "are you human", "not a bot",
             "making sure you", "just a sec", "verifying you", "checking if")
# bare publisher/site brand names that are NOT real titles
_SITEBRANDS = {"sciencedirect", "springer", "springerlink", "wiley", "wiley online library",
               "ssrn", "jstor", "researchgate", "academia.edu", "tandfonline", "mdpi",
               "ieee xplore", "acm digital library", "pubmed", "google scholar", "semantic scholar",
               "redirecting", "home", "homepage", "loading"}


def is_good_title(t, url):
    t = (t or "").strip(); tl = t.lower()
    if len(t) < 8 or any(b in tl for b in _BADTITLE):
        return False
    if tl.startswith("http") or tl in _SITEBRANDS:
        return False
    if " " not in t:                       # single token -> a domain/brand, not a title
        return False
    host = (urlparse(url).netloc or "").replace("www.", "").lower()
    bare = tl.replace("www.", "").rstrip("/")
    return not (bare == host or tl == host or (tl.startswith("www.") and "/" not in tl))


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--commit", action="store_true"); a = ap.parse_args()
    global _kms
    try:
        import lib.kms_knn as _kms  # title-based book/goods sorter (Bruno's bookmarks)
        _kms.classify("warmup", "https://amazon.com/dp/x")  # load model once up front
    except Exception:
        _kms = None
    pe = json.loads((ROOT / "panop_env.json").read_text(encoding="utf-8-sig"))
    H = {"Zotero-API-Key": pe["zotero_api_key"], "Zotero-API-Version": "3"}
    HW = {**H, "Content-Type": "application/json"}
    base = f"https://api.zotero.org/users/{pe['zotero_user_id']}"

    recs = {}
    for line in CKPT.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line); recs[r["key"]] = r
        except Exception:
            pass
    applied = set()
    if APPLIED.exists():
        applied = {l.strip() for l in APPLIED.read_text(encoding="utf-8").splitlines() if l.strip()}
    keys = [k for k in recs if k not in applied]
    print(f"classified: {len(recs)} | already applied: {len(applied)} | to process: {len(keys)}", flush=True)

    # fetch live version + collections + title in batches
    live = {}
    for i in range(0, len(keys), 50):
        ch = keys[i:i+50]
        r = requests.get(f"{base}/items?itemKey={','.join(ch)}&includeTrashed=1&limit=50", headers=H, timeout=40)
        if r.status_code == 200:
            for it in r.json():
                d = it.get("data", {})
                live[it["key"]] = {"version": it["version"], "collections": d.get("collections", []),
                                   "title": d.get("title", "") or ""}
        time.sleep(0.2)

    moves, retitles, trashes = Counter(), 0, 0
    plan = []
    for k in keys:
        rec = recs[k]; lv = live.get(k)
        if not lv:
            continue
        url = rec.get("url", ""); nc = rec.get("new_cat"); nt = rec.get("new_title", "")
        # Social/video hosts wall their body -> the engine returns None and they'd
        # wrongly stay in a scholarly collection (the X-links Bruno flagged). They
        # are never articles/books/science_news; default them to content_longform
        # (a YouTube lecture / X thread is read/watch-in-place). Bruno 2026-06-15.
        host = (urlparse(url).netloc or "").lower().replace("www.", "")
        SOCIAL = ("x.com", "twitter.com", "youtube.com", "youtu.be", "m.youtube.com",
                  "reddit.com", "facebook.com", "instagram.com", "tiktok.com", "linkedin.com")
        cur_scholarly = any(c in ("GKSJSJMJ", "B3XGDC4J", "BRZ3UUIR") for c in lv["collections"])
        if (nc in (None, "blocked", "needs_ai") or not nc) and cur_scholarly and \
           any(host == s or host.endswith("." + s) for s in SOCIAL):
            nc = "content_longform"
        # Amazon (& google-redirect-to-amazon) wall their body, so body:product
        # can't tell a BOOK ("Frege: Philosophy of Mathematics") from goods
        # ("Lava Louças 8 Serviços"). Don't be blunt — use the kNN trained on
        # Bruno's OWN bookmarks (his amazon books vs his amazon goods) to sort by
        # TITLE, but only when it's CONFIDENT (>=0.60); otherwise leave the item
        # where it is. Bruno 2026-06-17: be smarter, not all amazon = shopping.
        surl = (rec.get("surl") or url).lower()
        is_amazon = ("amazon." in surl or "amazon." in host
                     or "google.com/url" in url.lower() or host == "google.com")
        if is_amazon and nc in ("shopping", "books", None, "blocked", "needs_ai"):
            ttl = (rec.get("new_title") or lv.get("title") or "").strip()
            kk = _kms.classify(ttl, surl) if (ttl and _kms) else {}
            nc = kk.get("category") if (kk.get("confidence", 0) >= 0.60
                                        and kk.get("category") in ("books", "shopping")) else None
        patch, why = {}, []
        # TRASH true junk
        if nc == "reject":
            patch["deleted"] = 1; trashes += 1; why.append("trash:reject")
        else:
            # MOVE to correct collection
            if nc in CATKEY:
                tgt = CATKEY[nc]
                cur_panop = [c for c in lv["collections"] if c in PANOP_SUBTREE and c != "24A43HSI"]
                if tgt not in lv["collections"]:
                    newcols = [c for c in lv["collections"] if c not in PANOP_SUBTREE] + [tgt]
                    patch["collections"] = newcols
                    frm = KEY2CAT.get(cur_panop[0], "?") if cur_panop else "?"
                    moves[f"{frm}->{nc}"] += 1; why.append(f"move:{frm}->{nc}")
            # RETITLE junk titles only
            if is_good_title(nt, url) and not is_good_title(lv["title"], url):
                patch["title"] = nt[:300]; retitles += 1; why.append("retitle")
        if patch:
            plan.append((k, lv["version"], patch, why))

    print(f"\nPLAN over {len(keys)} items:")
    print(f"  moves: {sum(moves.values())}  {dict(moves.most_common(20))}")
    print(f"  retitles: {retitles}")
    print(f"  trashes (reject): {trashes}")
    print(f"  total items changed: {len(plan)}")

    if not a.commit:
        print("\nDRY RUN — pass --commit to apply (reversible: backup + Zotero version history/trash).")
        for k, v, p, why in plan[:15]:
            print(f"  {k}: {','.join(why)}")
        return

    BK.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    (BK / f"live_reclassify_premove_{stamp}.json").write_text(
        json.dumps({k: live[k] for k, *_ in plan}, ensure_ascii=False), encoding="utf-8")

    al = APPLIED.open("a", encoding="utf-8")
    ok = 0
    for k, ver, patch, why in plan:
        r = requests.patch(f"{base}/items/{k}", headers={**HW, "If-Unmodified-Since-Version": str(ver)},
                           data=json.dumps(patch), timeout=40)
        if r.status_code in (200, 204):
            ok += 1; al.write(k + "\n"); al.flush()
        elif r.status_code == 412:
            pass  # stale version — will be retried on a later run
        time.sleep(0.22)
    al.close()
    print(f"\nAPPLIED {ok}/{len(plan)} (backup live_reclassify_premove_{stamp}.json). Re-run to retry any 412s.")


if __name__ == "__main__":
    main()
