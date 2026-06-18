"""STEP 3 of the phone sweep (Bruno's design): with classification done ON THE
PC, save each fit to its destination, log rejects, and emit the list of tabs
that are safe to close. Closing happens in a SEPARATE step (phone_sweep_close)
AFTER everything here is durably saved.

Routing (Bruno 2026-06-14):
  articles / books / science_news  -> Zotero Panop/<col> + bookmark  (via the
                                       live /api/v1/history/add, which does both)
  science_longform                 -> Instapaper + bookmark queue (NOT Zotero)
  science_news + is_aggregator     -> recorded for 2nd-stage extraction
  reject                           -> reject ledger (URL preserved), then closable

Safety principles (every data op): restore point + full backup + per-item trace
+ reversible. Dry-run by default; --commit to act.
"""
from __future__ import annotations
import argparse, json, sys, time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
import requests

ROOT = Path(__file__).resolve().parents[1]
ST = ROOT / "state" / "panop"
TRACE_DIR = ST / "sweep_traces"
PANOP = "http://127.0.0.1:8000"
sys.path.insert(0, str(ROOT))


def _norm(u):
    return (u or "").strip()


def _resolve(url):
    """Resolve redirect-shaped URLs to their terminal target; leave others."""
    u = url.lower()
    if not any(s in u for s in ("news.google", "/url?", "t.co/", "lnkd.in",
                                "substack.com/redirect", "safelinks.protection",
                                "click.kit-mail", "bit.ly", "/l.php")):
        return url
    try:
        r = requests.head(url, allow_redirects=True, timeout=6)
        return r.url or url
    except Exception:
        try:
            r = requests.get(url, allow_redirects=True, timeout=8, stream=True)
            return r.url or url
        except Exception:
            return url


def load_labels():
    """Merge the three label sources -> {tab_url: {category, source, is_aggregator}}."""
    tabs = json.loads((ST / "phone_tabs_latest.json").read_text(encoding="utf-8"))
    by_id = {t["id"]: t for t in tabs}
    labels = {}
    # 1) confident signal labels {tab_id: category}
    conf = json.loads((ST / "phone_confident.json").read_text(encoding="utf-8"))
    for tid, cat in conf.items():
        if tid in by_id:
            labels[by_id[tid]["url"]] = {"category": cat, "source": "signal", "is_aggregator": False, "tab": by_id[tid]}
    # 2) rule labels (reject/longform) recomputed deterministically
    rule = json.loads((ST / "phone_rule_labels.json").read_text(encoding="utf-8"))
    for url, cat in rule.items():
        labels.setdefault(url, {"category": cat, "source": "rule", "is_aggregator": False,
                                "tab": next((t for t in tabs if t["url"] == url), {"url": url})})
    # 3) AI verdicts over the residual (index -> verdict; residual_sorted is the index base)
    res = json.loads((ST / "phone_residual_sorted.json").read_text(encoding="utf-8"))
    verdicts = json.loads((ST / "phone_ai_verdicts.json").read_text(encoding="utf-8"))
    for v in verdicts:
        i = v.get("idx")
        if i is None or i >= len(res):
            continue
        t = res[i]
        labels[t["url"]] = {"category": v["category"], "source": "ai",
                            "is_aggregator": bool(v.get("is_aggregator")), "tab": t,
                            "confidence": v.get("confidence"), "reason": v.get("reason")}
    return tabs, labels


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    TRACE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")

    tabs, labels = load_labels()
    print(f"tabs: {len(tabs)} | labeled: {len(labels)} | unlabeled: {len(tabs)-len(labels)}")
    from collections import Counter
    print("category split:", dict(Counter(v["category"] for v in labels.values())))
    aggs = [v for v in labels.values() if v["category"] == "science_news" and v["is_aggregator"]]
    print(f"science_news aggregators (2nd-stage): {len(aggs)}")

    # restore point: snapshot the inputs + a per-run manifest
    manifest = {"stamp": stamp, "tabs": len(tabs), "labels": len(labels)}
    (ST / f"sweep_restorepoint_{stamp}.json").write_text(
        json.dumps({"labels": {u: {k: vv for k, vv in v.items() if k != "tab"} for u, v in labels.items()}},
                   ensure_ascii=False), encoding="utf-8")

    if not args.commit:
        print("\nDRY RUN — no saves, no closes. Re-run with --commit.")
        return

    instapaper = None
    try:
        from lib.adapters import instapaper as _ip
        instapaper = _ip
    except Exception as e:
        print("instapaper import failed:", e)

    trace = open(TRACE_DIR / f"sweep_{stamp}.jsonl", "a", encoding="utf-8")
    reject_ledger = open(ST / f"reject_ledger_{stamp}.jsonl", "a", encoding="utf-8")
    closable, saved, rejected, agg_defer, failed = [], 0, 0, 0, 0
    items = list(labels.items())
    if args.limit:
        items = items[:args.limit]

    CATMAP = {"articles": "articles", "books": "books", "science_news": "science_news"}
    for url, v in items:
        cat = v["category"]
        tab = v.get("tab", {})
        tid = tab.get("id")
        title = (tab.get("title") or "").strip()
        final = _resolve(url)
        rec = {"ts": datetime.now().isoformat(), "tab_id": tid, "url": url,
               "final_url": final, "category": cat, "source": v["source"]}
        try:
            if cat == "reject":
                reject_ledger.write(json.dumps({**rec}, ensure_ascii=False) + "\n")
                rejected += 1
                rec["action"] = "rejected_logged"
                if tid:
                    closable.append(tid)
            elif cat == "science_news" and v["is_aggregator"]:
                agg_defer += 1
                rec["action"] = "aggregator_deferred"   # 2nd stage handles it; do NOT close yet
            elif cat in CATMAP:
                r = requests.post(f"{PANOP}/api/v1/history/add",
                                  json={"url": final, "title": title, "category_id": CATMAP[cat]}, timeout=25)
                ok = r.status_code == 200 and r.json().get("status") in ("ok", "already_exists")
                rec["action"] = "saved_zotero_bookmark"
                rec["save_status"] = (r.json() if r.status_code == 200 else r.status_code)
                if ok:
                    saved += 1
                    if tid:
                        closable.append(tid)
                else:
                    failed += 1
            elif cat == "science_longform":
                ok = False
                if instapaper:
                    res = instapaper.add_bookmark(final, title or None)
                    rec["instapaper"] = res.get("status")
                    ok = res.get("status") == "ok"
                # mirror bookmark
                try:
                    requests.post(f"{PANOP}/api/v1/history/add",
                                  json={"url": final, "title": title, "category_id": "science_longform"}, timeout=15)
                except Exception:
                    pass
                rec["action"] = "saved_instapaper_bookmark"
                if ok:
                    saved += 1
                    if tid:
                        closable.append(tid)
                else:
                    failed += 1
            time.sleep(0.05)
        except Exception as e:
            rec["error"] = str(e)[:160]
            failed += 1
        trace.write(json.dumps(rec, ensure_ascii=False) + "\n")
    trace.close(); reject_ledger.close()

    (ST / f"sweep_closable_{stamp}.json").write_text(json.dumps(closable), encoding="utf-8")
    (ST / "sweep_closable_latest.json").write_text(json.dumps(closable), encoding="utf-8")
    print(f"\nsaved={saved} rejected={rejected} aggregators_deferred={agg_defer} failed={failed}")
    print(f"closable tabs: {len(closable)} -> sweep_closable_latest.json")
    print(f"trace -> {TRACE_DIR / f'sweep_{stamp}.jsonl'}")


if __name__ == "__main__":
    main()
