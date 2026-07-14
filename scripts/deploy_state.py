"""Deploy-state truth report — 'what is fixed on disk but NOT live?'

Bruno 2026-07-13: the whole 'works only nominally' pattern was code fixed but
never running — stale Mouseion exe, unreloaded extension, services on cached
old code, config in the wrong place, false 'done' reports. This makes that gap
VISIBLE: for each deploy-sensitive component it reports LIVE / PENDING(action)
/ STALE, so no fix can silently fail to deploy again.

Read-only. Run anytime; surfaced in the morning brief.
"""
import json, os, subprocess, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
rows = []  # (state, component, detail, action)

def add(state, comp, detail, action=""):
    rows.append((state, comp, detail, action))

def _proc_starts():
    """{tag: earliest-start-epoch} for the long-running services, via PowerShell."""
    # Robust: emit SECONDS-SINCE-START per tag (avoids fragile epoch parsing),
    # Python converts to a start epoch. The prior %s/ToDateTime path silently
    # returned nothing → false 'DOWN' on live services (2026-07-13).
    ps = (
        "Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' } | "
        "ForEach-Object { $t='x'; "
        "if($_.CommandLine -match 'mind_service'){$t='mind'} "
        "elseif($_.CommandLine -match 'egon_core'){$t='core'} "
        "elseif($_.CommandLine -match 'search_worker'){$t='search'}; "
        "if($t -ne 'x'){ $age=[int]((Get-Date)-$_.CreationDate).TotalSeconds; "
        "Write-Output ($t+'|'+$age) } }"
    )
    now = time.time()
    out = {}
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=25)
        for line in (r.stdout or "").splitlines():
            if "|" in line:
                tag, age = line.strip().split("|", 1)
                try:
                    start = now - int(age)
                except ValueError:
                    continue
                out[tag] = max(out.get(tag, 0), start)  # newest instance
    except Exception:
        pass
    return out

def _newest_mtime(paths):
    m = 0.0
    for pat in paths:
        for p in ROOT.glob(pat):
            try: m = max(m, p.stat().st_mtime)
            except OSError: pass
    return m

def _sha(p, cap=8_000_000):
    import hashlib
    h = hashlib.md5()
    with open(p, "rb") as f:
        h.update(f.read(cap))
    return h.hexdigest()

starts = _proc_starts()

# 1. mind_service — running today's lib code?
code_m = _newest_mtime(["lib/*.py", "scripts/mind_service.py"])
if "mind" in starts:
    if starts["mind"] >= code_m:
        add("LIVE", "mind_service", "running current lib code")
    else:
        lag = (code_m - starts["mind"]) / 3600
        add("PENDING", "mind_service", f"code changed {lag:.1f}h after it started",
            "restart Egon (or bounce mind_service)")
else:
    add("DOWN", "mind_service", "not running", "start Egon")

# 2. egon_core — running today's scripts?
core_m = _newest_mtime(["scripts/egon_core.py", "lib/goal_tracker.py", "lib/*.py"])
if "core" in starts:
    if starts["core"] >= core_m:
        add("LIVE", "egon_core", "running current code")
    else:
        lag = (core_m - starts["core"]) / 3600
        add("PENDING", "egon_core", f"code changed {lag:.1f}h after it started",
            "restart Egon (loads goal driver, night rotation, etc.)")
else:
    add("DOWN", "egon_core", "not running", "start Egon")

# 3. Mouseion.exe — Desktop copy == freshest build?
desk = Path.home() / "Desktop" / "Mouseion.exe"
dist = Path.home() / "Desktop" / "mnt" / "outputs" / "zoterpile-main" / "dist" / "mouseion.exe"
try:
    if desk.exists() and dist.exists():
        if _sha(desk) == _sha(dist):
            add("LIVE", "Mouseion.exe", "Desktop == freshest build")
        else:
            add("STALE", "Mouseion.exe", "Desktop exe differs from newest build",
                "close Mouseion → swap dist build in")
    elif desk.exists():
        add("LIVE", "Mouseion.exe", "on Desktop (no newer build staged)")
except Exception as e:
    add("?", "Mouseion.exe", str(e)[:50])

# 4. Chrome extension — disk version (loaded copy needs a manual reload after edits)
try:
    ext = json.loads((ROOT / "external" / "egon_chrome_extension" / "manifest.json").read_text(encoding="utf-8"))
    add("INFO", "chrome-extension", f"disk v{ext.get('version')} — Chrome runs cached code until reloaded",
        "chrome://extensions → reload after any edit")
except Exception:
    pass

# 5. Mouseion goal driver — set to daemon AND loaded?
try:
    g = json.loads((ROOT / "state" / "goals.json").read_text(encoding="utf-8"))
    goals = g if isinstance(g, list) else g.get("goals", [])
    mg = next((x for x in goals if x.get("id") == "mouseion-8080"), {})
    drv = mg.get("driver")
    gt_m = (ROOT / "lib" / "goal_tracker.py").stat().st_mtime
    if drv == "mouseion_daemon":
        if "mind" in starts and starts["mind"] >= gt_m:
            add("LIVE", "goal-driver", "mouseion_daemon active")
        else:
            add("PENDING", "goal-driver", "driver set but service on old code",
                "restart Egon")
    else:
        add("INFO", "goal-driver", f"driver={drv}")
except Exception:
    pass

# 6. Pagefile — fixed size applied?
try:
    ps = ("$cs=Get-CimInstance Win32_ComputerSystem; "
          "$pf=Get-CimInstance Win32_PageFileSetting -EA SilentlyContinue; "
          "Write-Output ($cs.AutomaticManagedPagefile.ToString()+'|'+($(if($pf){$pf.InitialSize}else{0})))")
    r = subprocess.run(["powershell","-NoProfile","-Command",ps], capture_output=True, text=True, timeout=15)
    auto, init = (r.stdout or "|0").strip().split("|", 1)
    if auto.lower() == "false" and int(init or 0) > 0:
        add("PENDING", "pagefile", f"fixed {init}MB set — applies on reboot", "reboot to apply")
    else:
        add("INFO", "pagefile", "Windows-managed (dynamic)")
except Exception:
    pass

order = {"STALE":0,"PENDING":1,"DOWN":2,"?":3,"INFO":4,"LIVE":5}
rows.sort(key=lambda r: order.get(r[0], 9))
print("STATE     COMPONENT          DETAIL")
for st, comp, detail, action in rows:
    line = f"{st:9s} {comp:18s} {detail}"
    if action:
        line += f"\n{'':9s} {'':18s} → {action}"
    print(line)
pend = sum(1 for r in rows if r[0] in ("STALE","PENDING","DOWN"))
print(f"\n{pend} component(s) fixed-but-not-live. " +
      ("A single Egon restart + reboot lands them all." if pend else "Everything on disk is live."))
