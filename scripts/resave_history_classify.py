"""Re-classify the 1,129 rescued history items with the NEW body-first engine
(object-type + taxonomy), so they save into the right place. Bruno's move_to
corrections win where set; otherwise classify_by_body decides. Writes
state/panop/rescue_history_classified.json in the schema save_history.py reads:
  {url: {"category": <cat>, "title": <title>}}

Then:  python scripts/save_history.py --input state/panop/rescue_history_classified.json --dry
       python scripts/save_history.py --input state/panop/rescue_history_classified.json
"""
from __future__ import annotations
import sys, json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lib.body_classify import classify_by_body, resolve_redirect
from scripts.rescue_audit import canon

ST = ROOT / "state" / "panop"
SAVE = {"articles", "books", "science_news", "content_longform", "references",
        "data_tools", "shopping", "opportunities", "curios", "study_work"}


def main():
    work = json.loads((ST / "rescue_history_resave.json").read_text(encoding="utf-8"))
    # Bruno's manual category overrides (history items he recategorised)
    recat = {}
    rp = ST / "rescue_recategorize.json"
    if rp.exists():
        for r in json.loads(rp.read_text(encoding="utf-8")):
            recat[canon(r["url"])] = r["category"]
    print(f"worklist: {len(work)} | manual category overrides available: {len(recat)}", flush=True)

    def classify_one(it):
        url = resolve_redirect(it["url"]) or it["url"]   # follow redirect wrappers
        c = canon(it["url"])
        if c in recat:                                   # Bruno's call wins
            return it["url"], {"category": recat[c], "title": it.get("title") or url, "src": "bruno"}
        v = classify_by_body(url)
        cat = v.get("category")
        if cat in (None, "reject"):                      # walled/ambiguous -> default keep as longform
            cat = "content_longform" if cat is None else "reject"
        return it["url"], {"category": cat, "title": v.get("title") or it.get("title") or url, "src": v.get("source")}

    out, done = {}, 0
    with ThreadPoolExecutor(max_workers=12) as ex:
        futs = [ex.submit(classify_one, it) for it in work]
        for f in as_completed(futs):
            try:
                u, rec = f.result()
                out[u] = rec
            except Exception:
                pass
            done += 1
            if done % 100 == 0:
                print(f"  classified {done}/{len(work)}", flush=True)

    saveable = {u: r for u, r in out.items() if r["category"] in SAVE}
    from collections import Counter
    print(f"classified {len(out)} | saveable {len(saveable)} | "
          f"rejected {len(out)-len(saveable)}", flush=True)
    print("categories:", dict(Counter(r["category"] for r in out.values())), flush=True)
    (ST / "rescue_history_classified.json").write_text(
        json.dumps(saveable, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"-> {ST/'rescue_history_classified.json'}", flush=True)


if __name__ == "__main__":
    main()
