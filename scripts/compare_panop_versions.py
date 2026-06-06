"""Head-to-head perf comparison: pre-Antigravity main.py vs current main.py.

Goal: verify my Phase-1 changes preserve (or improve) the speed +
reliability gains Antigravity got by pruning ~900 lines from main.py.

For each version we spawn a fresh Python subprocess and measure:
  - module import time (cold)
  - process RSS after import (memory footprint)
  - uvicorn in-process boot time (time-to-first-healthy-response)
  - endpoint response latency (mean across the harvest endpoints)
  - route count

Then we print a side-by-side delta. Pre-Antigravity should be slower /
heavier (more code); current should be lighter but still serving every
endpoint Egon needs.

Run from the egon repo root:
    .venv\\Scripts\\python.exe scripts\\compare_panop_versions.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
CURRENT = ROOT / "external" / "panop_server" / "main.py"
BACKUP = ROOT / ".backups" / "panop_server_main_20260526_031348.py"


WORKER = r"""
import sys, time, json, os, threading, importlib.util
path = sys.argv[1]
port = int(sys.argv[2])

spec = importlib.util.spec_from_file_location("panop_under_test", path)
mod = importlib.util.module_from_spec(spec)
t0 = time.time()
try:
    spec.loader.exec_module(mod)
    import_dt = time.time() - t0
    import_ok = True
    import_err = ""
except Exception as e:
    import_dt = time.time() - t0
    import_ok = False
    import_err = f"{type(e).__name__}: {str(e)[:200]}"

try:
    import psutil
    rss = psutil.Process(os.getpid()).memory_info().rss / (1024*1024)
except Exception:
    rss = -1.0

route_count = len(list(getattr(mod, "app", None).routes)) if (import_ok and hasattr(mod, "app")) else 0

boot_dt = None
endpoint_results = {}
shutdown_dt = None
if import_ok and hasattr(mod, "app"):
    import uvicorn, requests
    cfg = uvicorn.Config(mod.app, host="127.0.0.1", port=port,
                         log_level="error", access_log=False)
    srv = uvicorn.Server(cfg)
    th = threading.Thread(target=srv.run, daemon=True); th.start()
    boot_t0 = time.time()
    for _ in range(80):
        try:
            r = requests.get(f"http://127.0.0.1:{port}/api/v1/status", timeout=0.5)
            if r.status_code == 200:
                boot_dt = time.time() - boot_t0
                break
        except Exception:
            pass
        time.sleep(0.2)

    if boot_dt is not None:
        for ep in ["/api/v1/status", "/api/v1/kindle/library",
                   "/api/v1/paperpile/library", "/api/v1/instapaper/library",
                   "/api/v1/tvtime/library", "/api/v1/youtube/history"]:
            tt = time.time()
            try:
                r = requests.get(f"http://127.0.0.1:{port}{ep}", timeout=2)
                endpoint_results[ep] = {"status": r.status_code,
                                        "ms": round((time.time()-tt)*1000, 1)}
            except Exception as e:
                endpoint_results[ep] = {"status": "err", "err": str(e)[:80]}

        # Shutdown timing
        s_t0 = time.time()
        srv.should_exit = True
        th.join(timeout=5)
        shutdown_dt = time.time() - s_t0

print("__RESULT_JSON_START__")
print(json.dumps({
    "import_ok": import_ok,
    "import_err": import_err,
    "import_dt_s": round(import_dt, 3),
    "rss_mb_after_import": round(rss, 1),
    "route_count": route_count,
    "boot_dt_s": round(boot_dt, 3) if boot_dt is not None else None,
    "shutdown_dt_s": round(shutdown_dt, 3) if shutdown_dt is not None else None,
    "endpoints": endpoint_results,
}, indent=2))
print("__RESULT_JSON_END__")
"""


def run_worker(path: Path, port: int) -> dict:
    p = subprocess.run([PY, "-c", WORKER, str(path), str(port)],
                       capture_output=True, text=True, timeout=60)
    out = p.stdout
    if "__RESULT_JSON_START__" not in out:
        return {"_error": "no result", "_stderr": p.stderr[:400]}
    j = out.split("__RESULT_JSON_START__", 1)[1].split("__RESULT_JSON_END__", 1)[0]
    try:
        return json.loads(j)
    except Exception as e:
        return {"_error": str(e), "_stdout": out[-400:]}


def fmt(v):
    return "n/a" if v is None else v


print("=" * 78)
print("Pre-Antigravity (backup)   vs   Current (Antigravity's prune + my restoration)")
print("=" * 78)

print(f"\nFile sizes:")
print(f"  backup : {BACKUP.stat().st_size:,} bytes  /  "
      f"{sum(1 for _ in open(BACKUP, encoding='utf-8'))} lines")
print(f"  current: {CURRENT.stat().st_size:,} bytes  /  "
      f"{sum(1 for _ in open(CURRENT, encoding='utf-8'))} lines")

print("\nRunning backup worker (port 8002)...")
a = run_worker(BACKUP, 8002)
print("Running current worker (port 8003)...")
c = run_worker(CURRENT, 8003)

if "_error" in a:
    print("backup worker FAILED:", a)
if "_error" in c:
    print("current worker FAILED:", c)

print("\nMetric                            | Backup (pre-AG) | Current (mine)  | Delta")
print("-" * 78)
def row(label, ka, kc, unit=""):
    va, vc = a.get(ka), c.get(kc if kc else ka)
    if isinstance(va, (int, float)) and isinstance(vc, (int, float)):
        delta = vc - va
        sign = "+" if delta >= 0 else ""
        delta_s = f"{sign}{delta:.2f}{unit}"
    else:
        delta_s = "—"
    print(f"  {label:<32}| {str(fmt(va)):<15} | {str(fmt(vc)):<15} | {delta_s}")

row("import time (s)", "import_dt_s", None)
row("RSS after import (MB)", "rss_mb_after_import", None)
row("uvicorn boot (s)", "boot_dt_s", None)
row("clean shutdown (s)", "shutdown_dt_s", None)
row("route count", "route_count", None)

print("\nEndpoint response times (ms, GET):")
eps = sorted(set((a.get("endpoints") or {}).keys()) |
             set((c.get("endpoints") or {}).keys()))
print(f"  {'endpoint':<32}| {'backup':<15} | {'current':<15} | delta")
print("-" * 78)
total_a, total_c, n_both = 0.0, 0.0, 0
for ep in eps:
    ea = (a.get("endpoints") or {}).get(ep, {})
    ec = (c.get("endpoints") or {}).get(ep, {})
    va, vc = ea.get("ms"), ec.get("ms")
    status_a, status_c = ea.get("status"), ec.get("status")
    va_s = f"{va} ({status_a})" if va is not None else f"({status_a})"
    vc_s = f"{vc} ({status_c})" if vc is not None else f"({status_c})"
    if isinstance(va, (int, float)) and isinstance(vc, (int, float)):
        delta = f"{vc-va:+.1f}"
        total_a += va; total_c += vc; n_both += 1
    else:
        delta = "—"
    print(f"  {ep:<32}| {va_s:<15} | {vc_s:<15} | {delta}")
if n_both:
    print(f"\n  mean endpoint latency:  backup={total_a/n_both:.1f}ms  "
          f"current={total_c/n_both:.1f}ms  delta={total_c/n_both - total_a/n_both:+.1f}ms")

# Verdict
print("\n" + "=" * 78)
verdicts = []
def vd(label, ka, kc, lower_is_better=True):
    va, vc = a.get(ka), c.get(kc if kc else ka)
    if not isinstance(va, (int, float)) or not isinstance(vc, (int, float)):
        verdicts.append((label, "?", "missing data"))
        return
    if lower_is_better:
        better = vc <= va
    else:
        better = vc >= va
    verdicts.append((label, "OK" if better else "REGRESSION",
                     f"backup={va} current={vc}"))

vd("import time", "import_dt_s", None, lower_is_better=True)
vd("RSS", "rss_mb_after_import", None, lower_is_better=True)
vd("boot time", "boot_dt_s", None, lower_is_better=True)
vd("shutdown time", "shutdown_dt_s", None, lower_is_better=True)
vd("route count (Egon-required endpoints)", "route_count", None, lower_is_better=False)
for label, status, detail in verdicts:
    print(f"  [{status}] {label}: {detail}")

regressions = [v for v in verdicts if v[1] == "REGRESSION"]
sys.exit(2 if regressions else 0)
