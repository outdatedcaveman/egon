"""Second rescue pass over the DISCARD set only — recover two real-content
classes the first pass mis-killed, and drop one false-restore:
  - google.com/url?...&url=<TARGET>  -> the wrapper is junk but TARGET is real;
    extract it and re-judge the target.
  - drive.google.com/file/d/<ID>     -> a specific shared file (Bruno's own
    PDFs), not the Drive app shell -> restore.
  - web3.arxiv.org (and other web3.* spam mirrors) -> remove from restore
    candidates (matched arxiv.org by suffix; it is crypto-spam, not arxiv).

Fast: DISCARD items were all decided by URL (no fetch); only the ~23 extracted
google/url targets are re-judged. Merges results into rescue_restore_candidates.

  python scripts/rescue_discard_pass2.py
"""
from __future__ import annotations
import sys, json
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.rescue_audit import (load_zotero_trash, canon, rescue_verdict, host_of)


def extract_google_target(url):
    try:
        q = parse_qs(urlparse(url).query)
        for k in ("url", "q", "u"):
            if q.get(k):
                t = unquote(q[k][0])
                if t.startswith("http") and "google.com" not in urlparse(t).netloc:
                    return t
    except Exception:
        pass
    return None


def main():
    pe = json.loads((ROOT / "panop_env.json").read_text(encoding="utf-8-sig"))
    items = load_zotero_trash(pe)
    for fn in ("history_harddelete.json", "history_junk_to_purge.json"):
        p = ROOT / "state" / "panop" / fn
        if p.exists():
            for j in json.loads(p.read_text(encoding="utf-8")):
                items.append({"src": fn.replace("history_", "").replace(".json", ""),
                              "key": None, "version": None,
                              "title": j.get("title", ""), "url": j.get("url", "")})
    seen, uniq = set(), []
    for it in items:
        c = canon(it["url"])
        if c and c not in seen:
            seen.add(c); uniq.append(it)

    rescued = []
    for it in uniq:
        v, src, rt = rescue_verdict(it["title"], it["url"], allow_fetch=False)
        if v != "DISCARD":
            continue
        u, host = it["url"], host_of(it["url"])
        # (a) google/url redirect -> extract + re-judge target
        if "google.com/url" in u:
            tgt = extract_google_target(u)
            if tgt:
                tv, tsrc, trt = rescue_verdict(it["title"], tgt, allow_fetch=True)
                if tv in ("RESTORE", "RESTORE?"):
                    rescued.append({**it, "url": tgt, "verdict": tv,
                                    "evidence": f"google_redirect->{tsrc}", "rtitle": trt or it["title"]})
        # (b) drive.google.com/file/d/ -> real shared file
        elif host.endswith("drive.google.com") and "/file/" in u:
            rescued.append({**it, "verdict": "RESTORE", "evidence": "drive_file",
                            "rtitle": it["title"]})

    # merge into restore candidates; drop web3.* false-positives
    cand_path = ROOT / "state" / "panop" / "rescue_restore_candidates.json"
    cands = json.loads(cand_path.read_text(encoding="utf-8"))
    before = len(cands)
    cands = [c for c in cands if not host_of(c["url"]).startswith("web3.")]
    dropped_web3 = before - len(cands)
    have = {canon(c["url"]) for c in cands}
    added = [r for r in rescued if canon(r["url"]) not in have]
    cands.extend(added)
    cand_path.write_text(json.dumps(cands, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"pass-2 rescued from DISCARD: {len(added)} "
          f"(google-redirect targets + drive files)")
    print(f"dropped web3.* false-restores: {dropped_web3}")
    print(f"restore candidates now: {len(cands)}")
    for r in added[:15]:
        print(f"  [{r['evidence']:22}] {(r['rtitle'] or '')[:46]:46} {r['url'][:46]}")


if __name__ == "__main__":
    main()
