"""End-to-end UI audit — exercise the REAL load path of each page + key actions.

Each page's on-open provider is called with a hard timeout, so a HANG is caught
and named (not left to freeze the app). Classifies:
  OK <ms>   — returns real data fast enough to feel instant (<1.5s)
  SLOW <ms> — works but >1.5s: feels broken on click (the '3-min Obsidian' class)
  HANG      — exceeded 20s: effectively frozen on use
  THIN      — wired but empty
  BROKEN    — raises on use
"""
import sys, time, json
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FTimeout
sys.path.insert(0, r"C:\Users\bruno\Claude Code\egon")

TIMEOUT = 20.0
rows = []
def probe(name, fn, thin=lambda v: False):
    def run():
        return fn()
    t0 = time.time()
    try:
        with ThreadPoolExecutor(max_workers=1) as ex:
            v = ex.submit(run).result(timeout=TIMEOUT)
        dt = (time.time() - t0)
        if thin(v):
            rows.append(("THIN", name, str(v)[:48], dt))
        elif dt > 1.5:
            rows.append(("SLOW", name, str(v)[:48], dt))
        else:
            rows.append(("OK", name, str(v)[:48], dt))
    except FTimeout:
        rows.append(("HANG", name, f">{TIMEOUT:.0f}s — frozen on use", TIMEOUT))
    except Exception as e:
        rows.append(("BROKEN", name, f"{type(e).__name__}: {str(e)[:60]}", time.time()-t0))

# DATABASES page load (the known 3-min obsidian hang lives here)
def _db_obsidian():
    from egon_app.pages.databases import _obsidian_stats
    return _obsidian_stats()
def _db_drift():
    from egon_app.pages.databases import _mirror_drift
    return f"{len(_mirror_drift())} drift rows"
def _db_gather():
    from egon_app.pages.databases import _gather
    g = _gather(); return f"files={g.get('files_n')} obs={bool(g.get('obsidian'))}"
probe("Databases/_obsidian_stats", _db_obsidian)
probe("Databases/_mirror_drift", _db_drift)
probe("Databases/_gather(full load)", _db_gather)

# NAVIGATION / Routster
def _nav():
    from lib.adapters import routster
    st = routster.live_status()
    links = routster.get_links()
    return f"status={bool(st)}, {len(links)} links"
probe("Navigation/routster", _nav, lambda v: "0 links" in v)

# LEDGER (token spend)
def _ledger():
    from lib.ledger import compute_ledger
    r = compute_ledger()
    return f"computed ({type(r).__name__})"
probe("Ledger/compute_ledger", _ledger)

# ARTIFACTS file explorer
def _artifacts():
    from egon_app.pages.artifacts import _load_rows
    return f"{len(_load_rows())} rows"
probe("Artifacts/_load_rows", _artifacts, lambda v: v.startswith("0 "))

# PROJECTS (mind project tree via HTTP)
def _projects():
    import urllib.request
    d = json.load(urllib.request.urlopen("http://127.0.0.1:8000/api/v1/mind/projects", timeout=15))
    n = len(d.get("projects") or d if isinstance(d, (list,)) else (d.get("projects") or []))
    return f"{n} projects"
probe("Projects/mind-projects", _projects, lambda v: v.startswith("0 "))

# DISCOVERY (watcher queue read — NOT run_watchers, which has side effects)
def _discovery():
    from lib.discovery_watchers import QUEUE_PATH
    import os, json as _j
    if not os.path.exists(QUEUE_PATH):
        return "no queue file"
    q = _j.load(open(QUEUE_PATH, encoding="utf-8"))
    return f"{len(q) if isinstance(q,(list,dict)) else '?'} queued"
probe("Discovery/queue", _discovery, lambda v: "no queue" in v or v.startswith("0 "))

# HOME (whatever its refresh aggregates — snapshot summary)
def _home():
    from lib import state
    d = state.load() if hasattr(state, "load") else {}
    return f"state keys={len(d) if isinstance(d,dict) else '?'}"
probe("Home/state", _home)

order = {"HANG":0,"BROKEN":1,"SLOW":2,"THIN":3,"OK":4}
rows.sort(key=lambda r: (order.get(r[0],9), -r[3]))
print("STATUS   SURFACE                        DETAIL")
for s, n, d, dt in rows:
    print(f"{s:8s} {n:30s} {d}  [{dt*1000:.0f}ms]")
from collections import Counter
c = Counter(r[0] for r in rows)
print("\nSUMMARY:", " · ".join(f"{v} {k}" for k,v in sorted(c.items(), key=lambda x: order.get(x[0],9))))
