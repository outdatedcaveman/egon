"""Recover REAL titles for Cloudflare-walled papers via metadata APIs, not by
scraping the walled page. ~1,187 Zotero items still show "Just a moment…";
~531 carry a DOI in their URL -> look the title up via Crossref (no walling) and
PATCH Zotero. Reversible (Zotero keeps version history; originals logged).

  python scripts/retitle_via_metadata.py            # dry-run: how many recovered
  python scripts/retitle_via_metadata.py --commit
"""
from __future__ import annotations
import sys, json, re, time, argparse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import requests
from scripts.apply_live_library import is_good_title

ST = ROOT / "state" / "panop"
CKPT = ST / "live_reclassify.jsonl"
LEDGER = ST / "retitle_metadata_ledger.jsonl"
_DOI = re.compile(r"\b(10\.\d{4,9}/[^\s?#&\"']+)", re.I)
_MAILTO = "bruno.saramago.monteiro@gmail.com"
UA = {"User-Agent": f"EgonKMS/1.0 (mailto:{_MAILTO})"}


def doi_of(url):
    m = _DOI.search(url or "")
    if not m:
        return None
    d = m.group(1).rstrip(".)/")
    # strip common trailing artifacts (pdf, /full, /abstract, query)
    d = re.split(r"[?#]|/full|/abstract|/pdf|\.pdf", d, 1)[0].rstrip("./")
    return d


def crossref_title(doi):
    try:
        r = requests.get(f"https://api.crossref.org/works/{doi}", headers=UA, timeout=12)
        if r.status_code == 200:
            t = (r.json().get("message", {}).get("title") or [""])[0]
            return re.sub(r"\s+", " ", t).strip()
    except Exception:
        pass
    return ""


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--commit", action="store_true"); a = ap.parse_args()
    pe = json.loads((ROOT / "panop_env.json").read_text(encoding="utf-8-sig"))
    H = {"Zotero-API-Key": pe["zotero_api_key"], "Zotero-API-Version": "3"}
    HW = {**H, "Content-Type": "application/json"}
    base = f"https://api.zotero.org/users/{pe['zotero_user_id']}"

    # walled-title items that carry a DOI
    items, seen = [], set()
    for line in CKPT.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r["key"] in seen:
            continue
        ot = (r.get("old_title") or "").strip().lower()
        if not ot.startswith("just a moment"):
            continue
        d = doi_of(r.get("surl") or r.get("url") or "")
        if d:
            seen.add(r["key"]); items.append({"key": r["key"], "doi": d, "url": r.get("url", "")})
    done = set()
    if LEDGER.exists():
        done = {json.loads(l)["key"] for l in LEDGER.read_text(encoding="utf-8").splitlines() if l.strip()}
    items = [it for it in items if it["key"] not in done]
    print(f"walled items with DOI to look up: {len(items)}", flush=True)

    # Crossref lookups (concurrent, polite)
    recovered = {}
    def look(it):
        return it["key"], crossref_title(it["doi"])
    n = 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        for f in as_completed([ex.submit(look, it) for it in items]):
            k, t = f.result()
            if is_good_title(t, "") and len(t) > 8:
                recovered[k] = t
            n += 1
            if n % 100 == 0:
                print(f"  looked up {n}/{len(items)} (recovered {len(recovered)})", flush=True)
    print(f"\nreal titles recovered via Crossref: {len(recovered)} / {len(items)}", flush=True)
    for k, t in list(recovered.items())[:10]:
        print(f"  {t[:75]}")

    if not a.commit:
        print("\nDRY RUN — pass --commit to PATCH Zotero titles (reversible).")
        return

    # fetch live versions, PATCH titles
    keys = list(recovered)
    ver = {}
    for i in range(0, len(keys), 50):
        ch = keys[i:i+50]
        r = requests.get(f"{base}/items?itemKey={','.join(ch)}&includeTrashed=1&limit=50", headers=H, timeout=40)
        if r.status_code == 200:
            for it in r.json():
                ver[it["key"]] = it["version"]
        time.sleep(0.2)
    led = LEDGER.open("a", encoding="utf-8")
    ok = 0
    for k in keys:
        if k not in ver:
            continue
        r = requests.patch(f"{base}/items/{k}", headers={**HW, "If-Unmodified-Since-Version": str(ver[k])},
                           data=json.dumps({"title": recovered[k][:300]}), timeout=40)
        if r.status_code in (200, 204):
            ok += 1; led.write(json.dumps({"key": k, "title": recovered[k]}) + "\n"); led.flush()
        time.sleep(0.2)
    led.close()
    print(f"\nRETITLED {ok}/{len(keys)} papers with real Crossref titles (reversible).")


if __name__ == "__main__":
    main()
