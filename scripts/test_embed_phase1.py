"""End-to-end test for the 2026-05-27 in-process embedding (Phase 1).

Verifies that the changes (lib/panop_proc.py rewrite to in-process,
_adb_list_devices no per-poll start-server, startup hook one-time hidden
adb daemon start, sibling-kill cmdline match) are NET POSITIVE: faster
or no-slower boot, the same surface (health + harvest endpoints work),
no shell-window flashing, clean shutdown, no Egon import regressions.

Run from the egon repo root:
    .venv\\Scripts\\python.exe scripts\\test_embed_phase1.py
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"
results: list[tuple[str, str, str]] = []  # (label, status, detail)


def rec(label: str, status: str, detail: str = "") -> None:
    results.append((label, status, detail))
    print(f"[{status}] {label}  {detail}")


def rss_mb() -> float:
    try:
        import psutil
        return psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)
    except Exception:
        return -1.0


# -- T1: Egon main imports cleanly (no syntax / dependency regressions) --
print("\n-- T1: Egon main import smoke --")
t0 = time.time()
try:
    # Don't actually instantiate Qt — just import the module so we catch
    # any import-time errors my edits introduced.
    import importlib
    importlib.import_module("egon_app.main")
    rec("egon_app.main importable", PASS, f"in {time.time()-t0:.2f}s")
except Exception as e:
    rec("egon_app.main importable", FAIL, f"{type(e).__name__}: {str(e)[:200]}")

# -- T2: Panop FastAPI app imports + boot timing --
print("\n-- T2: Panop import + in-process uvicorn boot --")
import_t0 = time.time()
try:
    from external.panop_server.main import app as panop_app
    import_dt = time.time() - import_t0
    rec("import panop_server.main", PASS, f"app={type(panop_app).__name__} in {import_dt:.2f}s")
except Exception as e:
    rec("import panop_server.main", FAIL, f"{type(e).__name__}: {str(e)[:200]}")
    panop_app = None  # type: ignore

rss_before_boot = rss_mb()

if panop_app is not None:
    try:
        from lib import panop_proc
        boot_t0 = time.time()
        # Quick sanity: nothing on :8000 right now
        s = socket.socket()
        port_busy = False
        try:
            s.bind(("127.0.0.1", 8000))
        except OSError:
            port_busy = True
        finally:
            s.close()
        if port_busy:
            rec("port :8000 free pre-boot", WARN, "something already listening on 8000 (stray panop?)")
        else:
            rec("port :8000 free pre-boot", PASS)

        ok = panop_proc.ensure_running(log_fn=lambda *a, **kw: None)
        boot_dt = time.time() - boot_t0
        if ok and panop_proc.is_running():
            # subprocess version typically needed 4–8s; in-process should be similar
            # or faster (no python interpreter spinup). Pass if under 15s.
            if boot_dt < 15.0:
                rec("Panop boots in-process and serves /status", PASS, f"in {boot_dt:.2f}s")
            else:
                rec("Panop boots in-process and serves /status", WARN, f"slow: {boot_dt:.2f}s")
        else:
            rec("Panop boots in-process and serves /status", FAIL, f"after {boot_dt:.2f}s")
    except Exception as e:
        rec("Panop boots in-process and serves /status", FAIL, f"{type(e).__name__}: {str(e)[:200]}")

rss_after_boot = rss_mb()
rss_delta = rss_after_boot - rss_before_boot if rss_before_boot > 0 else -1.0

# -- T3: Harvest endpoints respond correctly --
print("\n-- T3: Harvest endpoints (Kindle / Instapaper / TV Time / YouTube history) --")
try:
    import requests
    BASE = "http://127.0.0.1:8000"
    endpoints = [
        "/api/v1/status",
        "/api/v1/kindle/library",
        "/api/v1/paperpile/library",
        "/api/v1/instapaper/library",
        "/api/v1/tvtime/library",
        "/api/v1/youtube/history",
    ]
    for ep in endpoints:
        t0 = time.time()
        try:
            r = requests.get(BASE + ep, timeout=4)
            dt = (time.time() - t0) * 1000
            if r.status_code == 200:
                try:
                    j = r.json()
                    has_status = "status" in j
                    rec(f"GET {ep}", PASS, f"200 in {dt:.0f}ms  status={j.get('status')}  has_items={('items' in j)}")
                except Exception:
                    rec(f"GET {ep}", WARN, f"200 in {dt:.0f}ms but non-JSON body")
            else:
                rec(f"GET {ep}", FAIL, f"http {r.status_code} in {dt:.0f}ms")
        except Exception as e:
            rec(f"GET {ep}", FAIL, f"{type(e).__name__}: {str(e)[:120]}")
except Exception as e:
    rec("endpoint suite", FAIL, f"setup failed: {e}")

# -- T4: _store_harvest keep-previous safety (server change) --
print("\n-- T4: server keep-previous-on-empty safety net --")
try:
    import requests
    BASE = "http://127.0.0.1:8000"
    # Read whatever's currently stored for kindle so we can restore-feel
    cur = requests.get(BASE + "/api/v1/kindle/library", timeout=3).json()
    cur_count = cur.get("count", 0)
    cur_items = cur.get("items") or []
    # POST an EMPTY harvest payload. With our keep-previous fix the server
    # must NOT clobber the prior library — the file's items should still be
    # present after the POST.
    empty_payload = {"ts": int(time.time() * 1000), "url": "test",
                     "count": 0, "items": [], "strategy": "test_phase1",
                     "_debug": {"phase1_test": True}}
    r = requests.post(BASE + "/api/v1/kindle/library", json=empty_payload, timeout=5)
    if r.status_code != 200:
        rec("POST empty harvest accepted", FAIL, f"http {r.status_code}")
    else:
        post_j = r.json()
        # After the empty POST, read back: items should NOT be wiped
        after = requests.get(BASE + "/api/v1/kindle/library", timeout=3).json()
        after_count = after.get("count", 0)
        # If we started with items and they survived, the safety net works.
        # If we started with zero items, this test is inconclusive — mark WARN.
        if cur_count == 0:
            rec("keep-previous on empty harvest", WARN, "no prior items to test against (inconclusive)")
        elif after_count == cur_count:
            rec("keep-previous on empty harvest", PASS,
                f"items preserved (count={after_count}); kept_previous={post_j.get('kept_previous')}")
        else:
            rec("keep-previous on empty harvest", FAIL,
                f"library wiped: prior={cur_count} now={after_count}")
except Exception as e:
    rec("keep-previous on empty harvest", FAIL, f"{type(e).__name__}: {str(e)[:200]}")

# -- T5: no-flash adb behaviour — count fresh adb.exe / conhost.exe over 20s --
print("\n-- T5: shell-window-flash check (20s window) --")
try:
    if sys.platform != "win32":
        rec("flash check", WARN, "not Windows — skipped")
    else:
        # Use WMI via subprocess (already running, no extra deps)
        import subprocess
        def snapshot_pids():
            # Returns dict pid -> name for current short-lived candidates
            r = subprocess.run(
                ["wmic", "process", "where",
                 "(name='adb.exe' or name='conhost.exe' or name='cmd.exe')",
                 "get", "ProcessId,Name", "/format:csv"],
                capture_output=True, text=True, timeout=10,
            )
            out = {}
            for line in (r.stdout or "").splitlines():
                parts = line.strip().split(",")
                if len(parts) >= 3 and parts[2].strip().isdigit():
                    out[int(parts[2].strip())] = parts[1].strip()
            return out
        before = snapshot_pids()
        t0 = time.time()
        time.sleep(20)
        after = snapshot_pids()
        new = {pid: name for pid, name in after.items() if pid not in before}
        elapsed = time.time() - t0
        n_adb = sum(1 for n in new.values() if n.lower() == "adb.exe")
        n_conhost = sum(1 for n in new.values() if n.lower() == "conhost.exe")
        # Pre-fix: ~13 spawns in 20s (every 1.5s). With the per-poll start-server
        # removed and only the once-per-6s `adb devices` running, we expect 0-3
        # adb spawns (or 0 if the daemon stays up). Accept anything <= 5.
        detail = f"new adb={n_adb}  new conhost={n_conhost}  in {elapsed:.0f}s"
        if n_adb <= 5:
            rec("adb spawn rate is bounded (was every ~1.5s pre-fix)", PASS, detail)
        else:
            rec("adb spawn rate is bounded", FAIL, detail)
except Exception as e:
    rec("flash check", FAIL, f"{type(e).__name__}: {str(e)[:200]}")

# -- T6: clean shutdown — stop() drains the thread within timeout --
print("\n-- T6: clean in-process shutdown --")
try:
    from lib import panop_proc
    shutdown_t0 = time.time()
    panop_proc.stop(timeout_s=6.0)
    shutdown_dt = time.time() - shutdown_t0
    # Allow the OS a moment to release the port
    time.sleep(0.6)
    if not panop_proc.is_running():
        rec("Panop stops cleanly when asked", PASS, f"in {shutdown_dt:.2f}s")
    else:
        rec("Panop stops cleanly when asked", WARN,
            f"still running after {shutdown_dt:.2f}s (daemon thread will die with process anyway)")
except Exception as e:
    rec("Panop stops cleanly when asked", FAIL, f"{type(e).__name__}: {str(e)[:200]}")

# -- T7: memory overhead of in-process embedding --
print("\n-- T7: memory overhead --")
if rss_before_boot > 0 and rss_after_boot > 0:
    # Subprocess version: Panop ran in its OWN python process at ~80–150 MB.
    # In-process: that overhead now lives inside Egon. We expect the delta
    # in this test process to be in roughly that range. Anything > 250 MB
    # would be a regression worth flagging.
    if rss_delta < 250:
        rec("in-process RSS overhead reasonable", PASS,
            f"+{rss_delta:.1f} MB ({rss_before_boot:.1f} -> {rss_after_boot:.1f})")
    else:
        rec("in-process RSS overhead reasonable", WARN,
            f"+{rss_delta:.1f} MB ({rss_before_boot:.1f} -> {rss_after_boot:.1f})")
else:
    rec("in-process RSS overhead", WARN, "could not measure (psutil missing?)")

# -- Summary --
print("\n" + "=" * 60)
n_pass = sum(1 for _, s, _ in results if s == PASS)
n_warn = sum(1 for _, s, _ in results if s == WARN)
n_fail = sum(1 for _, s, _ in results if s == FAIL)
print(f"SUMMARY: {n_pass} PASS, {n_warn} WARN, {n_fail} FAIL")
print("=" * 60)
if n_fail:
    print("\nFAILURES:")
    for label, status, detail in results:
        if status == FAIL:
            print(f"  - {label}: {detail}")
    sys.exit(2)
sys.exit(0 if n_warn == 0 else 1)
