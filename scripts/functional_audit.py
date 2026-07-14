"""Functional audit — 'does it actually deliver when used', not 'is it wired'.

Bruno 2026-07-13: "a lot of things work in Egon only nominally, or until we try
to use it for the first time." This EXERCISES each surface's real data provider
(what the page renders) and classifies:
  OK    — returns real data / performs the action
  THIN  — wired but empty/stale (looks alive, delivers little)
  BROKEN— raises / times out / returns an error on use
Read-only + lightweight (HTTP GETs + adapter calls); no heavy compute.
"""
import sys, time, json, traceback
sys.path.insert(0, r"C:\Users\bruno\Claude Code\egon")

results = []
def probe(name, fn, thin_if=lambda v: False):
    t0 = time.time()
    try:
        v = fn()
        dt = int((time.time() - t0) * 1000)
        if thin_if(v):
            results.append(("THIN", name, f"{v}", dt))
        else:
            results.append(("OK", name, f"{v}", dt))
    except Exception as e:
        dt = int((time.time() - t0) * 1000)
        results.append(("BROKEN", name, f"{type(e).__name__}: {str(e)[:70]}", dt))

def http(url, timeout=6):
    import urllib.request
    r = urllib.request.urlopen(url, timeout=timeout)
    return r.status, len(r.read())

# ── MEDIA adapters (7) — real item counts ──
def _mediacount(mod, fn="items", *a):
    m = __import__(f"lib.adapters.{mod}", fromlist=["x"])
    items = getattr(m, fn)(*a) if a else getattr(m, fn)()
    return len(items) if isinstance(items, list) else items
probe("media/letterboxd", lambda: _mediacount("letterboxd", "items", 5000), lambda v: isinstance(v,int) and v < 5)
probe("media/tvtime",     lambda: _mediacount("tvtime", "items", 500), lambda v: isinstance(v,int) and v < 5)
probe("media/pocketcasts",lambda: _mediacount("pocketcasts", "podcasts"), lambda v: isinstance(v,int) and v < 3)
probe("media/instapaper", lambda: _mediacount("instapaper", "items", 5000), lambda v: isinstance(v,int) and v < 5)
probe("media/kindle",     lambda: _mediacount("kindle", "items", 5000), lambda v: isinstance(v,int) and v < 5)

# ── SEARCH (:8801 worker) — a real query must return hits ──
def _search():
    import urllib.request
    req = urllib.request.Request("http://127.0.0.1:8801/connect",
        data=json.dumps({"text":"category theory","limit":3}).encode(),
        headers={"Content-Type":"application/json"})
    d = json.load(urllib.request.urlopen(req, timeout=60))
    return f"{d.get('count',0)} hits, mode={d.get('mode')}"
probe("search/connect(:8801)", _search, lambda v: "0 hits" in str(v))

# ── MIND service (:8000) — stats + real memory/activity counts ──
probe("mind/stats(:8000)", lambda: http("http://127.0.0.1:8000/api/v1/mind/stats"))
def _minddb():
    import sqlite3
    c = sqlite3.connect("file:state/mind.db?mode=ro", uri=True, timeout=5)
    mem = c.execute("SELECT COUNT(*) FROM memory").fetchone()[0]
    act = c.execute("SELECT COUNT(*) FROM activity").fetchone()[0]
    c.close()
    return f"{mem} memories, {act} activity"
probe("mind/db-content", _minddb)

# ── REFERENCES (Zotero/Mouseion) ──
def _refs():
    import sqlite3
    from pathlib import Path
    db = Path.home()/".local"/"share"/"mouseion"/"refs.db"
    c = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=8)
    n = c.execute("SELECT COUNT(*) FROM refs").fetchone()[0]
    c.close(); return f"{n:,} refs"
probe("references/mouseion", _refs)

# ── DATABASES observatory (Notion/Obsidian mirror) ──
def _mirror(kind):
    import glob, os
    d = {"notion":"state/snapshots/notion_workspace","obsidian":str(__import__('pathlib').Path.home()/ 'Documents'/'Obsidian Vault'/'050 - Mirrors')}[kind]
    if kind=="notion":
        s = sorted(glob.glob(d+"/*.json"), reverse=True)
        if not s: return "no snapshot"
        j = json.load(open(s[0],encoding="utf-8"))
        age = (time.time()-os.path.getmtime(s[0]))/3600
        return f"{j.get('count','?')} pages, {age:.0f}h old"
    else:
        n = sum(len(files) for _,_,files in os.walk(d)) if os.path.isdir(d) else 0
        return f"{n} mirror files"
probe("databases/notion-mirror", lambda: _mirror("notion"), lambda v: "no snapshot" in str(v))
probe("databases/obsidian-mirror", lambda: _mirror("obsidian"), lambda v: v=="0 mirror files")

# ── ORCHESTRATOR agents ──
def _orch():
    import sqlite3
    c = sqlite3.connect("file:state/mind.db?mode=ro", uri=True, timeout=5)
    n = c.execute("SELECT COUNT(*) FROM orchestrator_tasks WHERE created_at > strftime('%s','now')-7*86400").fetchone()[0]
    ok = c.execute("SELECT COUNT(*) FROM orchestrator_tasks WHERE status='completed' AND created_at > strftime('%s','now')-7*86400").fetchone()[0]
    c.close(); return f"{n} tasks/7d, {ok} completed"
probe("orchestrator/tasks", _orch, lambda v: v.startswith("0 "))

# ── INBOX / Panop drain ──
def _panop():
    import glob, os
    s = sorted(glob.glob("state/panop/*.json"), key=os.path.getmtime, reverse=True)
    if not s: return "no panop state"
    age = (time.time()-os.path.getmtime(s[0]))/3600
    return f"latest panop state {age:.0f}h old"
probe("inbox/panop", _panop)

# ── PERSONA ──
def _persona():
    import glob
    for p in ("state/persona.json","state/persona_interests.json"):
        import os
        if os.path.exists(p):
            j = json.load(open(p,encoding="utf-8"))
            return f"{len(j) if isinstance(j,(list,dict)) else '?'} entries"
    return "no persona file"
probe("persona", _persona, lambda v: "no persona" in str(v))

# ── print classified ──
order = {"BROKEN":0,"THIN":1,"OK":2}
results.sort(key=lambda r: order.get(r[0],3))
print("STATUS   SURFACE                     DETAIL")
for status, name, detail, dt in results:
    print(f"{status:8s} {name:27s} {detail}  [{dt}ms]")
b = sum(1 for r in results if r[0]=="BROKEN"); t = sum(1 for r in results if r[0]=="THIN"); o=sum(1 for r in results if r[0]=="OK")
print(f"\nSUMMARY: {o} OK · {t} THIN · {b} BROKEN  (of {len(results)} surfaces)")
