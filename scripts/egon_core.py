"""Egon Core — the always-on supervisor for Egon's substrate services.

Bruno 2026-06-12: the mind died twice in one day and the Headroom proxy was
found down despite a directive that every agent route through it. Root cause:
each service was supervised by whichever app happened to start it (or nothing).
This is the fix — ONE supervised core, independent of the desktop UI, that
keeps the substrate alive and visibly healthy:

  • Mind service (:8000) — spawns scripts/mind_service.py (idempotent, has its
    own mutex) whenever /api/v1/mind/stats stops answering ok.
  • Headroom proxy (:8787) — via lib.headroom_proc.ensure_running (Antigravity's
    supervisor, reused; CREATE_NO_WINDOW, Python-degraded mode flag injected).
    Previously only alive while the Egon app was open — now always-on.
  • Semantic Connect index — lib.semantic_index.build(force=False) every 6 h
    (incremental by content hash, cheap after first build).
  • Health visibility — state/core_health.json rewritten every cycle with the
    live status of each unit + restart counts; logs/egon-core.log structured.

Lifecycle: started at login by the Startup launcher (the sanctioned always-on
exception to the no-daemons rule — it IS the coordination substrate). Single
instance enforced by kernel mutex. Check cycle every 30 s, restart backoff per
unit so a crash-looping service can't spin the CPU.

Run:  pythonw.exe scripts\\egon_core.py
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    import lib.no_console  # noqa: F401
except Exception:
    pass

# Launch services with the BASE interpreter, NOT the .venv redirector stub.
# Bruno 2026-06-25: core roles must not be loose wrapper-parent processes.
from lib.python_runtime import base_python, runtime_env  # noqa: E402

PYW = base_python(ROOT, windowed=True)
SPAWN_ENV = runtime_env(ROOT)
LOG = ROOT / "logs" / "egon-core.log"
HEALTH = ROOT / "state" / "core_health.json"

CHECK_EVERY_S = 30
INDEX_EVERY_S = 6 * 3600
RESTART_BACKOFF_S = 120          # per-unit: at most one restart per 2 min
# Default "idle": the hands-off KMS work (hydrate every document's full text,
# embed it, refresh the index, mirrors) runs ONLY after the PC has been idle for
# HEAVY_IDLE_AFTER_S — so it never competes with active use (the freeze that made
# this "manual" came from heavy work running WHILE Bruno used the PC; idle mode
# can't do that). Bruno greenlit idle-aware whole-vault embedding 2026-06-24.
# Set EGON_CORE_HEAVY_MODE=off to pause, =always to ignore the idle gate.
HEAVY_MODE = os.environ.get("EGON_CORE_HEAVY_MODE", "idle").strip().lower()
HEAVY_IDLE_AFTER_S = int(os.environ.get("EGON_CORE_HEAVY_IDLE_AFTER_S", "900"))

MIND_STATS = "http://127.0.0.1:8000/api/v1/mind/stats"
HEADROOM_HEALTH = "http://127.0.0.1:8787/health"
MOBILE_CONNECT_PORT = 8765
OLLAMA_TAGS = "http://127.0.0.1:11434/api/tags"
OLLAMA_EXE = Path.home() / "AppData/Local/Programs/Ollama/ollama.exe"  # no hardcoded user path


def log(level: str, event: str, **kw) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        line = (datetime.now().isoformat(timespec="seconds")
                + f" [{level}] event={event} "
                + " ".join(f"{k}={v}" for k, v in kw.items()))
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line.rstrip() + "\n")
    except Exception:
        pass


def _http_ok(url: str, timeout: float = 3.0) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = r.read(400).decode("utf-8", "replace")
        return r.status == 200, body
    except Exception as e:
        return False, f"{type(e).__name__}"


def _tcp_ok(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _idle_seconds() -> float:
    """Seconds since last keyboard/mouse input. Windows-only; conservative."""
    if os.name != "nt":
        return 0.0
    try:
        import ctypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            return 0.0
        tick = ctypes.windll.kernel32.GetTickCount()
        return max(0.0, (tick - lii.dwTime) / 1000.0)
    except Exception:
        return 0.0


MIN_FREE_GB = float(os.environ.get("EGON_CORE_MIN_FREE_GB", "6"))
# Min available system RAM before heavy corpus work may start. Tuned to this
# 8GB machine that maxes out ~1GB free even idle: model2vec is light and the
# 23GB pagefile absorbs spikes (the full re-embed already completed this way), so
# 1.0GB lets the autonomous work actually run while still refusing to start when
# RAM is critically low. Bruno 2026-06-24 ("we'll have to adjust and work with it").
MIN_FREE_RAM_GB = float(os.environ.get("EGON_CORE_MIN_FREE_RAM_GB", "1.0"))


def _free_gb(path: Path) -> float:
    try:
        import shutil
        return shutil.disk_usage(str(path)).free / (1024 ** 3)
    except Exception:
        return 999.0


def _free_ram_gb() -> float:
    """Available physical RAM in GB (Windows GlobalMemoryStatusEx)."""
    try:
        import ctypes

        class _MS(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong),
                        ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        ms = _MS(); ms.dwLength = ctypes.sizeof(_MS)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):
            return ms.ullAvailPhys / (1024 ** 3)
    except Exception:
        pass
    return 999.0


def _heavy_allowed() -> tuple[bool, str]:
    """Gate CPU/network-heavy background work.

    The always-on core must be a supervisor first. Heavy corpus work is useful,
    but it cannot run just because Egon is alive: it was freezing the PC while
    Bruno was actively using Inbox. Defaults to manual opt-in.
    """
    # HARD disk guard: whole-vault hydration/embedding grows the index by many GB.
    # Never run it when free space is low, or it would fill the disk and wedge the
    # machine. Check the SYSTEM drive (C:) — even when the index lives on Google
    # Drive, Drive streams through a local cache on C:, so C: is the real
    # constraint. Take the min with the index drive too, for a true second disk.
    # Bruno 2026-06-24.
    try:
        from lib.egon_paths import CONNECT_INDEX_DIR as _idx_dir
        free = min(_free_gb(ROOT), _free_gb(Path(_idx_dir)) if Path(_idx_dir).exists() else 999.0)
    except Exception:
        free = _free_gb(ROOT)
    if free < MIN_FREE_GB:
        return False, f"paused: low disk ({free:.1f}GB free < {MIN_FREE_GB}GB)"
    ram = _free_ram_gb()
    if ram < MIN_FREE_RAM_GB:
        return False, f"paused: low RAM ({ram:.1f}GB avail < {MIN_FREE_RAM_GB}GB) — would OOM"
    if HEAVY_MODE in ("1", "true", "yes", "always"):
        return True, "heavy mode always"
    if HEAVY_MODE in ("off", "0", "false", "no", "manual", ""):
        return False, "paused: set EGON_CORE_HEAVY_MODE=idle or always"
    if HEAVY_MODE == "idle":
        idle = _idle_seconds()
        if idle >= HEAVY_IDLE_AFTER_S:
            return True, f"idle {int(idle)}s"
        return False, f"paused: active user, idle {int(idle)}s/{HEAVY_IDLE_AFTER_S}s"
    return False, f"paused: unknown heavy mode {HEAVY_MODE!r}"


# ── units ────────────────────────────────────────────────────────────────────
class Unit:
    # Consecutive probe failures required before a unit counts as down.
    # 2026-06-11 post-mortem: 14 phantom "mind restarts" in one day — every
    # single one found the service already running. The probe (3s, one
    # strike) was timing out while the mind was merely BUSY (MiniLM encode
    # for /connect saturates the process for a few seconds). Busy ≠ dead.
    FAILS_REQUIRED = 3

    def __init__(self, name: str):
        self.name = name
        self.ok = False
        self.detail = ""
        self.restarts = 0
        self.last_restart = 0.0
        self.fails = 0

    def probe(self, ok: bool) -> bool:
        """Record a probe result; True only when down is CONFIRMED."""
        self.fails = 0 if ok else self.fails + 1
        return self.fails >= self.FAILS_REQUIRED

    def can_restart(self) -> bool:
        return time.time() - self.last_restart > RESTART_BACKOFF_S

    def mark_restart(self):
        self.restarts += 1
        self.last_restart = time.time()
        self.fails = 0

    def as_dict(self) -> dict:
        return {"ok": self.ok, "detail": self.detail, "restarts": self.restarts}


def check_mind(u: Unit) -> None:
    t0 = time.time()
    ok, body = _http_ok(MIND_STATS, timeout=8.0)
    ok = ok and '"status":"ok"' in body.replace(" ", "")
    ms = int((time.time() - t0) * 1000)
    # A TIMEOUT means the process is ALIVE but busy — the connect index warm-up
    # (MiniLM load + a 330MB meta.json parse + 1.7GB index) holds the GIL for
    # ~3 min, so the stats probe times out even though mind is fine. Restarting
    # it then KILLS the warm-up before it finishes, so the cache never persists
    # and every phone search is cold (30-55s flapping). Only a refused/aborted
    # connection (process actually gone) counts toward a restart; a timeout does
    # not. Bruno 2026-06-24.
    busy = (not ok) and ("timeout" in body.lower())
    if busy:
        u.fails = 0
        confirmed_down = False
    else:
        confirmed_down = u.probe(ok)
    u.ok = ok or not confirmed_down   # busy-but-alive still counts as ok
    u.detail = (f"serving ({ms}ms)" if ok
                else f"busy ({ms}ms)" if busy
                else f"slow/failed probe {u.fails}/{u.FAILS_REQUIRED}: {body}")
    if ms > 2000 or not ok:
        log("info", "mind_probe_slow", ms=ms, ok=ok, busy=busy, fails=u.fails)
    if not confirmed_down or not u.can_restart():
        return
    u.mark_restart()
    log("warn", "mind_down_restarting", attempt=u.restarts)
    try:
        # mind_service is idempotent (own mutex + ready checks); detached so it
        # outlives the core if the core itself is restarted.
        env = {**SPAWN_ENV, "EGON_MIND_SERVICE_FORCE": "1"}
        subprocess.Popen(
            [str(PYW), str(ROOT / "scripts" / "mind_service.py")],
            cwd=str(ROOT), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008),
        )
    except Exception as e:
        log("error", "mind_spawn_failed", error=str(e)[:160])


def check_mobile_connect(u: Unit) -> None:
    ok = _tcp_ok("127.0.0.1", MOBILE_CONNECT_PORT, timeout=0.5)
    confirmed_down = u.probe(ok)
    u.ok = ok or not confirmed_down
    u.detail = "serving" if ok else f"probe {u.fails}/{u.FAILS_REQUIRED}"
    if not confirmed_down or not u.can_restart():
        return
    u.mark_restart()
    log("warn", "mobile_connect_down_restarting", attempt=u.restarts)
    try:
        env = {**SPAWN_ENV, "EGON_MIND_SERVICE_FORCE": "1"}
        subprocess.Popen(
            [str(PYW), str(ROOT / "scripts" / "mind_service.py")],
            cwd=str(ROOT), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008),
        )
    except Exception as e:
        log("error", "mobile_connect_spawn_failed", error=str(e)[:160])


def check_headroom(u: Unit) -> None:
    ok, body = _http_ok(HEADROOM_HEALTH, timeout=6.0)
    confirmed_down = u.probe(ok)
    u.ok = ok or not confirmed_down
    u.detail = ("healthy" + (" (python-degraded)" if "disabled" in body else "")
                ) if ok else f"probe {u.fails}/{u.FAILS_REQUIRED}: {body}"
    if not confirmed_down or not u.can_restart():
        return
    u.mark_restart()
    log("warn", "headroom_down_restarting", attempt=u.restarts)
    try:
        from lib import headroom_proc
        headroom_proc.ensure_running(
            log_fn=lambda lvl, **kw: log(lvl, kw.pop("event", "headroom"), **kw))
    except Exception as e:
        log("error", "headroom_start_failed", error=str(e)[:160])


def check_ollama(u: Unit) -> None:
    """Keep the local synthesis brain (Ollama, qwen2.5:3b) serving. The model
    itself loads on demand and auto-unloads when idle, so a running server
    costs almost nothing. Bruno 2026-06-12 (#2: retrieval → answers)."""
    ok, _ = _http_ok(OLLAMA_TAGS, timeout=6.0)
    confirmed_down = u.probe(ok)
    u.ok = ok or not confirmed_down
    u.detail = "serving" if ok else f"probe {u.fails}/{u.FAILS_REQUIRED}"
    if not confirmed_down or not u.can_restart():
        return
    if not OLLAMA_EXE.exists():
        u.detail = "ollama not installed"
        return
    u.mark_restart()
    log("warn", "ollama_down_restarting", attempt=u.restarts)
    try:
        subprocess.Popen(
            [str(OLLAMA_EXE), "serve"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008
                           | 0x08000000),
        )
    except Exception as e:
        log("error", "ollama_spawn_failed", error=str(e)[:160])


_index_building = False
_index_last = 0.0
_hydrating = False
_hydrate_proc = None      # isolated hydration/OCR subprocess
_index_proc = None        # isolated index-rebuild subprocess
_index_backup_date = ""   # last day the local index was mirrored to Drive
_index_backup_proc = None  # isolated dated-zip Drive backup subprocess
_reembed_proc = None
_reembed_last_swap = 0.0


def _any_heavy_running() -> bool:
    """True if any heavy subprocess is alive — used to SERIALIZE heavy jobs so
    only ONE runs at a time. Two/three concurrent (OCR + index + concept) is
    what blew the 8GB box past 12GB. Bruno 2026-06-30."""
    g = globals()
    for name in ("_hydrate_proc", "_index_proc", "_reembed_proc", "_concept_proc",
                 "_mirror_proc", "_index_backup_proc", "_canonical_proc",
                 "_exhaustive_proc"):
        p = g.get(name)
        try:
            if p is not None and p.poll() is None:
                return True
        except Exception:
            pass
    return False


HEAVY_REAP_IDLE_S = int(os.environ.get("EGON_HEAVY_REAP_IDLE_S", "45"))


def _reap_heavy(reason: str = "user-active") -> int:
    """Terminate EVERY heavy subprocess immediately. Called the instant the user
    becomes active (heavy work must NEVER thrash the 8GB box while Bruno uses it)
    and at startup to clear orphans a prior egon_core instance left running (e.g.
    a catchup Notion loop). This is the hard guarantee that Egon's heavy jobs are
    strictly idle-only. Bruno 2026-07-01."""
    g = globals()
    killed = 0
    for name in ("_hydrate_proc", "_index_proc", "_reembed_proc", "_concept_proc",
                 "_mirror_proc", "_index_backup_proc", "_canonical_proc",
                 "_exhaustive_proc"):
        p = g.get(name)
        try:
            if p is not None and p.poll() is None:
                p.kill()
                killed += 1
            g[name] = None
        except Exception:
            pass
    # Sweep orphaned -c workers (untracked, from prior instances)
    try:
        mypid = os.getpid()
        out = subprocess.run(
            ["wmic", "process", "where", "name like '%python%'",
             "get", "ProcessId,CommandLine"],
            capture_output=True, text=True).stdout
        import re as _re
        for line in out.splitlines():
            low = line.lower()
            if " -c " in line and any(k in low for k in (
                    "mirror_runner", "run_notion", "concept_graph", "semantic_index",
                    "auto_hydrate", "hydration_worker", "notion_body", "reembed")):
                mm = _re.search(r"(\d+)\s*$", line.strip())
                if mm and int(mm.group(1)) != mypid:
                    subprocess.run(["taskkill", "/PID", mm.group(1), "/F"],
                                   capture_output=True)
                    killed += 1
    except Exception:
        pass
    if killed:
        log("info", "heavy_reaped", n=killed, reason=reason)
    return killed
# Re-embed the WHOLE corpus at most this often — model2vec is cheap (minutes),
# so a periodic full pass keeps picking up newly hydrated document text with no
# incremental-build complexity. 12h.
REEMBED_COOLDOWN_S = int(os.environ.get("EGON_REEMBED_COOLDOWN_S", str(12 * 3600)))
# Presence of this file = "keep the model2vec index fresh in idle windows".
# Delete it to stop. The cycle is hands-off + abortable; no daemon. Bruno 2026-06-24.
REEMBED_TRIGGER = ROOT / "state" / "reembed_active.json"


def _prune_old_index_backups(keep: int = 2) -> None:
    """Bound Drive usage: keep only the `keep` newest connect_index_bak_* dirs
    (old model-index snapshots are regenerable, not user data)."""
    try:
        import shutil
        from lib.egon_paths import CONNECT_INDEX_DIR as _idx
        parent = Path(_idx).parent
        baks = sorted(parent.glob(Path(_idx).name + "_bak_*"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
        for old in baks[keep:]:
            shutil.rmtree(old, ignore_errors=True)
    except Exception:
        pass


def check_reembed(u: "Unit") -> None:
    """Drive the streaming bge-base re-embed (lib/reembed) in idle windows, as an
    ISOLATED subprocess so its ~500MB model RSS is freed every cycle (never
    bloats this supervisor on the 8GB box). Builds into a STAGING dir — the live
    index is untouched until a deliberate swap. Resumable + RAM/idle-guarded."""
    global _reembed_proc
    if not REEMBED_TRIGGER.exists():
        u.ok = True
        u.detail = "off"
        return
    try:
        from lib import reembed
        st = reembed.status()
    except Exception as e:
        u.ok = True
        u.detail = f"err {str(e)[:60]}"
        return
    global _reembed_last_swap
    prog = f"[{st.get('done', 0)}/{st.get('total', '?')}]"
    if st.get("state") == "complete":
        # AUTONOMOUS swap: promote the freshly-built index live (mind_service
        # auto-reloads it via meta.json mtime), back up the old one, drop staging
        # so a later cycle re-embeds again — folding in newly hydrated document
        # text. 100% local (no quota), so the vault keeps deepening hands-off.
        try:
            ts = time.strftime("%Y%m%d-%H%M%S")
            res = reembed.swap_in(ts)
            _prune_old_index_backups(keep=2)
            _reembed_last_swap = time.time()
            log("info", "reembed_swapped", **res)
            u.ok = True
            u.detail = f"swapped live ({st.get('items')} items) @ {ts}"
        except Exception as e:
            u.ok = True
            u.detail = f"swap failed: {str(e)[:60]}"
        return
    if _reembed_proc is not None and _reembed_proc.poll() is None:
        u.ok = True
        u.detail = f"embedding… {prog}"
        return
    if _any_heavy_running():
        u.ok = True
        u.detail = f"queued (another heavy job) {prog}"
        return
    # Cooldown between full re-embed passes (a fresh pass picks up new doc text).
    since = time.time() - _reembed_last_swap
    if _reembed_last_swap and since < REEMBED_COOLDOWN_S:
        u.ok = True
        u.detail = f"fresh (next pass in {int((REEMBED_COOLDOWN_S - since)/3600)}h)"
        return
    allowed, why = _heavy_allowed()
    if not allowed:
        u.ok = True
        u.detail = f"idle-wait ({why}) {prog}"
        return
    # Launch a bounded, self-terminating batch. idle_abort_s bails the moment the
    # user returns; max_seconds caps the run; the model RSS dies with the process.
    if _any_heavy_running():
        u.ok = True
        u.detail = f"waiting (another heavy job) {prog}"
        return
    code = ("import sys; sys.path.insert(0, r'{root}'); from lib import reembed; "
            "print(reembed.reembed(max_seconds=1800, ram_floor_gb=0.8, "
            "idle_abort_s=90))").format(root=str(ROOT))
    try:
        _reembed_proc = subprocess.Popen(
            [str(PYW), "-c", code], cwd=str(ROOT), env=SPAWN_ENV,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008))
        u.ok = True
        u.detail = f"launched batch {prog}"
        log("info", "reembed_batch_launched", **{k: st.get(k) for k in ("done", "total")})
    except Exception as e:
        u.ok = True
        u.detail = f"launch failed: {str(e)[:60]}"


_concept_proc = None
_concept_last = 0.0
CONCEPT_COOLDOWN_S = int(os.environ.get("EGON_CONCEPT_COOLDOWN_S", str(24 * 3600)))

_canonical_proc = None
_canonical_last = 0.0
CANONICAL_COOLDOWN_S = int(os.environ.get("EGON_CANONICAL_COOLDOWN_S", str(3 * 3600)))

_exhaustive_proc = None
_exhaustive_last = 0.0
EXHAUSTIVE_COOLDOWN_S = int(os.environ.get("EGON_EXHAUSTIVE_COOLDOWN_S", str(6 * 3600)))


def check_exhaustive(u: "Unit") -> None:
    """The COMPREHENSIVE/EXHAUSTIVE mind guarantee (Bruno 2026-07-01: 'NOTHING
    AT ALL that factors into my Claude, Antigravity and Codex use should be
    left out'). lib/mind_exhaustive: byte-mirrors every AI-home file into
    state/mind_archive (before the apps prune their own history), manifests
    everything in mind.db, parses the stores the old ingest missed (Antigravity
    conversations, Codex sqlite threads+memories, Claude history/plans/tasks),
    and backfills full transcripts where the old 200-event cap dropped the
    middle. Idle-gated, serialized, isolated subprocess with idle-abort."""
    global _exhaustive_proc, _exhaustive_last
    if _exhaustive_proc is not None and _exhaustive_proc.poll() is None:
        u.ok = True
        u.detail = "capturing everything…"
        return
    since = time.time() - _exhaustive_last
    if _exhaustive_last and since < EXHAUSTIVE_COOLDOWN_S:
        # coverage summary for visibility between runs
        try:
            cov = json.loads((ROOT / "state" / "mind_coverage.json").read_text(
                encoding="utf-8"))
            tot = sum(a.get("files_seen", 0) for a in cov.get("agents", {}).values())
            arc = sum(a.get("archived", 0) for a in cov.get("agents", {}).values())
            u.detail = f"covered {arc}/{tot} files (next in {int((EXHAUSTIVE_COOLDOWN_S - since)/3600)}h)"
        except Exception:
            u.detail = f"cooldown ({int((EXHAUSTIVE_COOLDOWN_S - since)/3600)}h)"
        u.ok = True
        return
    allowed, why = _heavy_allowed()
    if not allowed:
        u.ok = True
        u.detail = f"idle-wait ({why})"
        return
    if _any_heavy_running():
        u.ok = True
        u.detail = "waiting (another heavy job)"
        return
    code = (
        "import sys, ctypes; sys.path.insert(0, r'{root}')\n"
        "def _idle_s():\n"
        "    class L(ctypes.Structure):\n"
        "        _fields_ = [('cbSize', ctypes.c_uint), ('dwTime', ctypes.c_uint)]\n"
        "    li = L(); li.cbSize = ctypes.sizeof(L)\n"
        "    ctypes.windll.user32.GetLastInputInfo(ctypes.byref(li))\n"
        "    return (ctypes.windll.kernel32.GetTickCount() - li.dwTime) / 1000.0\n"
        "from lib import mind_exhaustive\n"
        "r = mind_exhaustive.run_exhaustive(stop_check=lambda: _idle_s() < 30)\n"
        "print(r.get('coverage', {{}}).get('agents'))\n"
    ).format(root=str(ROOT))
    try:
        _exhaustive_proc = subprocess.Popen(
            [str(PYW), "-c", code], cwd=str(ROOT), env=SPAWN_ENV,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008))
        _exhaustive_last = time.time()
        u.ok = True
        u.detail = "exhaustive capture launched"
        log("info", "exhaustive_capture_launched")
    except Exception as e:
        u.ok = True
        u.detail = f"launch failed: {str(e)[:60]}"


def check_canonical(u: "Unit") -> None:
    """Egon builds the CANONICAL project structure from every AI's work: it
    classifies each newly-ingested session by CONTENT (hybrid embedding + LLM,
    lib/canonical_classifier) into a canonical project, then materializes the
    browsable tree under ~/AI/projects (lib/canonical_fs). Idle-gated, isolated
    subprocess, serialized with the other heavy jobs. This is the source Egon's
    chat + all AIs ground on — not the messy app repos. Bruno 2026-07-01."""
    global _canonical_proc, _canonical_last
    if _canonical_proc is not None and _canonical_proc.poll() is None:
        u.ok = True
        u.detail = "classifying sessions…"
        return
    # anything unclassified?
    try:
        import sqlite3
        from lib.mind_context_broker import DB_PATH
        c = sqlite3.connect(str(DB_PATH), timeout=5)
        if c.execute("SELECT name FROM sqlite_master WHERE name='canonical_assignments'"
                     ).fetchone():
            pending = c.execute(
                "SELECT COUNT(*) FROM sessions WHERE CAST(id AS TEXT) NOT IN "
                "(SELECT item_id FROM canonical_assignments WHERE item_type='session')"
            ).fetchone()[0]
            pending += c.execute(
                "SELECT COUNT(*) FROM memory WHERE superseded_by_memory_id IS NULL "
                "AND LENGTH(COALESCE(content,'')) >= 30 AND CAST(id AS TEXT) NOT IN "
                "(SELECT item_id FROM canonical_assignments WHERE item_type='memory')"
            ).fetchone()[0]
        else:
            pending = c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        c.close()
    except Exception as e:
        u.ok = True
        u.detail = f"db err {str(e)[:30]}"
        return
    if pending <= 0:
        u.ok = True
        u.detail = "canonical up to date"
        return
    since = time.time() - _canonical_last
    if _canonical_last and since < CANONICAL_COOLDOWN_S:
        u.ok = True
        u.detail = f"{pending} pending (cooldown)"
        return
    allowed, why = _heavy_allowed()
    if not allowed:
        u.ok = True
        u.detail = f"{pending} pending (idle-wait: {why})"
        return
    if _any_heavy_running():
        u.ok = True
        u.detail = "waiting (another heavy job)"
        return
    code = ("import sys; sys.path.insert(0, r'{root}');"
            "from lib import canonical_classifier as cc, canonical_fs;"
            "cc.classify_sessions(limit=None, use_llm=True, only_unclassified=True);"
            "cc.classify_memories(limit=None, use_llm=True, only_unclassified=True);"
            "print(canonical_fs.export_canonical())").format(root=str(ROOT))
    try:
        _canonical_proc = subprocess.Popen(
            [str(PYW), "-c", code], cwd=str(ROOT), env=SPAWN_ENV,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008))
        _canonical_last = time.time()
        u.ok = True
        u.detail = f"classifying {pending} sessions"
        log("info", "canonical_classify_launched", pending=pending)
    except Exception as e:
        u.ok = True
        u.detail = f"launch failed: {str(e)[:60]}"


def check_concept_graph(u: "Unit") -> None:
    """Rebuild the higher-order Concept Graph (lib/concept_graph) when the live
    embedding index is newer than the last graph — idle-gated, in an ISOLATED
    subprocess (its meta-title load briefly needs ~1GB, freed when the process
    dies). This is the data behind the Categorical Mind / CatColab graphic home:
    concepts clustered from the whole embedded vault + their morphisms. Local,
    no quota. Bruno 2026-06-25."""
    global _concept_proc, _concept_last
    meta = ROOT / "state" / "connect_index" / "meta.json"
    cg = ROOT / "state" / "concept_graph.json"
    # Drive-backed index: prefer the env path the engine itself uses.
    try:
        from lib.egon_paths import CONNECT_INDEX_DIR
        meta = CONNECT_INDEX_DIR / "meta.json"
    except Exception:
        pass
    cg_age = cg.stat().st_mtime if cg.exists() else 0
    idx_age = meta.stat().st_mtime if meta.exists() else 0
    stale = (not cg.exists()) or (idx_age > cg_age)
    if not stale:
        u.ok = True
        u.detail = f"fresh ({int((time.time()-cg_age)/3600)}h old)"
        return
    if _concept_proc is not None and _concept_proc.poll() is None:
        u.ok = True
        u.detail = "clustering concepts…"
        return
    since = time.time() - _concept_last
    if _concept_last and since < CONCEPT_COOLDOWN_S:
        u.ok = True
        u.detail = f"cooldown ({int((CONCEPT_COOLDOWN_S - since)/3600)}h)"
        return
    allowed, why = _heavy_allowed()
    if not allowed:
        u.ok = True
        u.detail = f"idle-wait ({why})"
        return
    if _any_heavy_running():
        u.ok = True
        u.detail = "waiting (another heavy job)"
        return
    code = ("import sys; sys.path.insert(0, r'{root}'); from lib import concept_graph; "
            "r = concept_graph.build_concept_graph(k=200); "
            "print(r.get('n_concepts'), r.get('n_items'))").format(root=str(ROOT))
    try:
        _concept_proc = subprocess.Popen(
            [str(PYW), "-c", code], cwd=str(ROOT), env=SPAWN_ENV,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008))
        _concept_last = time.time()
        u.ok = True
        u.detail = "rebuild launched"
        log("info", "concept_graph_rebuild_launched")
    except Exception as e:
        u.ok = True
        u.detail = f"launch failed: {str(e)[:60]}"


def check_hermes(u: "Unit") -> None:
    """Lean always-on oversight (Hermes monitor): each cycle it scans orchestrator
    task health, agent quota cooldowns, and cross-AI opportunities, and SURFACES
    masterlaw-screened proposals to state/hermes_proposals.json for Bruno's
    console. Read-only DB + small JSON write — no model, no quota, negligible
    RAM/CPU. It PROPOSES only; autonomous dispatch stays gated. Bruno 2026-06-24."""
    try:
        from lib import hermes_monitor
        res = hermes_monitor.run_once()
        u.ok = True
        u.detail = res.get("summary", "scanned")
    except Exception as e:
        u.ok = True
        u.detail = f"err {str(e)[:60]}"


def check_hydration(u: "Unit") -> None:
    """Continuously extract full text from indexed documents while the PC is
    idle, so every doc's CONTENT (not just its filename) becomes searchable.
    Runs a bounded batch per cycle in a background thread, bailing the instant
    the user returns; the 6-hourly index rebuild embeds the accumulated
    extracts. This is the engine of the whole-vault embedding goal. 2026-06-24."""
    global _hydrate_proc
    allowed, why = _heavy_allowed()
    if not allowed:
        u.ok = True
        u.detail = f"idle-wait ({why})"
        return
    if _hydrate_proc is not None and _hydrate_proc.poll() is None:
        u.ok = True
        u.detail = "hydrating… (isolated subprocess)"
        return
    if _any_heavy_running():
        u.ok = True
        u.detail = "queued (another heavy job running)"
        return
    if _any_heavy_running():
        u.ok = True
        u.detail = "waiting (another heavy job running)"
        return
    # Run extraction + OCR in an ISOLATED subprocess so PaddleOCR's models and
    # extraction buffers are FREED when it exits each batch. In-thread they
    # accumulated multiple GB inside this always-on supervisor (it grew to
    # ~6.8GB private over a few days, thrashing the 8GB box). Bruno 2026-06-30.
    # The subprocess polls real input idle and ABORTS within ~1s of any
    # keypress/mouse move — so it never churns/freezes the PC while Bruno is
    # using it. It only runs during genuine idle; the 15-min start gate is in
    # _heavy_allowed. Smaller batch (200 docs / 0.8GB) keeps each unit light.
    # Bruno 2026-06-30.
    code = (
        "import sys, ctypes\n"
        "sys.path.insert(0, r'{root}')\n"
        "def _idle_s():\n"
        "    class L(ctypes.Structure): _fields_=[('s',ctypes.c_uint),('t',ctypes.c_uint)]\n"
        "    l=L(); l.s=ctypes.sizeof(l)\n"
        "    ctypes.windll.user32.GetLastInputInfo(ctypes.byref(l))\n"
        "    return (ctypes.windll.kernel32.GetTickCount()-l.t)/1000.0\n"
        "from lib import auto_hydrate_crawler as ah\n"
        "print(ah.run_crawler(max_extracts=200, max_bytes=int(8e8), "
        "stop_check=lambda: _idle_s() < 30))\n"
    ).format(root=str(ROOT))
    try:
        _hydrate_proc = subprocess.Popen(
            [str(PYW), "-c", code], cwd=str(ROOT), env=SPAWN_ENV,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008))
        u.ok = True
        u.detail = "batch launched (idle-only, aborts on activity)"
    except Exception as e:
        u.ok = True
        u.detail = f"launch failed: {str(e)[:60]}"


def check_index(u: Unit) -> None:
    """Refresh the semantic Connect index every INDEX_EVERY_S, off-thread."""
    global _index_proc, _index_last
    meta = ROOT / "state" / "connect_index" / "meta.json"
    age = (time.time() - meta.stat().st_mtime) if meta.exists() else None
    u.ok = age is not None
    u.detail = (f"age={int(age // 60)}m" if age is not None else "not built")
    allowed, why = _heavy_allowed()
    if not allowed:
        u.detail += f" ({why})"
        return
    if _index_proc is not None and _index_proc.poll() is None:
        u.detail += " (refreshing, subprocess)"
        return
    if _any_heavy_running():
        u.detail += " (queued: heavy job running)"
        return
    due = age is None or age > INDEX_EVERY_S
    if not due or time.time() - _index_last < 600:
        return
    if _any_heavy_running():
        u.detail += " (waiting: another heavy job)"
        return
    _index_last = time.time()
    # Run the WHOLE index cycle (pinned-file extract, OCR crawl, file index,
    # the 984k-item embedding rebuild, Obsidian mirror) in an ISOLATED
    # subprocess. In-thread these loaded the embedding model + corpus + OCR into
    # this always-on supervisor and never returned the memory to the OS — the
    # core of the 6.8GB bloat. A subprocess frees it all on exit. Bruno 2026-06-30.
    code = (
        "import sys; sys.path.insert(0, r'{root}')\n"
        # NOTE: extraction/OCR is handled solely by check_hydration's subprocess
        # — do NOT run the crawler here too (it spawned a 2nd OCR process).
        "for step in ("
        "  lambda: __import__('lib.hydration_worker', fromlist=['x']).process_queue(),"
        "  lambda: __import__('lib.file_indexer', fromlist=['x']).build(),"
        "  lambda: __import__('lib.semantic_index', fromlist=['x']).build(force=False),"
        "  lambda: __import__('lib.obsidian_mirror', fromlist=['x']).mirror_all()):\n"
        "    try: step()\n"
        "    except Exception as e: print('step failed:', e)\n"
    ).format(root=str(ROOT))
    try:
        _index_proc = subprocess.Popen(
            [str(PYW), "-c", code], cwd=str(ROOT), env=SPAWN_ENV,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008))
        u.detail += " (rebuild launched, subprocess)"
        log("info", "index_cycle_launched")
    except Exception as e:
        u.detail += f" (launch failed: {str(e)[:50]})"


DIGEST_JSON = ROOT / "state" / "daily_digest.json"
DIGEST_MD = ROOT / "state" / "daily_digest.md"
DIGEST_AFTER_HOUR = 8          # generate once per day, first cycle after 08:00
_digest_running = False

_DRIVE_INDEX_BACKUP = Path(os.environ.get(
    "EGON_INDEX_DRIVE_BACKUP", r"G:\My Drive\EgonData\connect_index"))
_DRIVE_EXTRACTS_BACKUP = Path(os.environ.get(
    "EGON_EXTRACTS_DRIVE_BACKUP", r"G:\My Drive\EgonData\file_extracts"))


def _backup_private_config(day: str) -> None:
    """Daily: zip private config + customizations (keys, persona, settings) to a
    dated file on Drive for safekeeping. Tiny + fast, so inline (no subprocess).
    New dated file each day = no Drive in-place-overwrite conflict. Bruno 2026-07-01."""
    try:
        import zipfile
        import glob as _glob
        patterns = ["egon-config.json", ".env", "state/persona*.json",
                    "state/settings*.json", "state/*config*.json",
                    "state/memory_rules*.json", "state/notion_catchup_active.json",
                    "state/reembed_active.json", "state/hydrate_cloud.json",
                    "external/panop_server/panop_env.json"]
        files = sorted({Path(p) for pat in patterns
                        for p in _glob.glob(str(ROOT / pat)) if Path(p).is_file()})
        if not files:
            return
        bdir = _DRIVE_INDEX_BACKUP.parent / "config_backup"
        bdir.mkdir(parents=True, exist_ok=True)
        zpath = bdir / f"egon_private_{day}.zip"
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            for f in files:
                z.write(f, f.relative_to(ROOT))
        for old in sorted(_glob.glob(str(bdir / "egon_private_*.zip")))[:-5]:
            try:
                os.remove(old)
            except Exception:
                pass
        log("info", "private_config_backup", files=len(files), day=day)
    except Exception as e:
        log("warn", "private_config_backup_failed", error=str(e)[:120])


def check_index_backup(u: "Unit") -> None:
    """The live index + extracts now live on LOCAL disk (fast, no Drive sync
    thrash). Once a day, mirror them to the Drive copy as a backup — robocopy in
    a detached process, incremental (only changed files upload). Skips if the
    index is still the Drive copy (nothing to back up to itself). Bruno 2026-06-30."""
    global _index_backup_date
    today = datetime.now().strftime("%Y-%m-%d")
    if _index_backup_date == today:
        u.ok = True
        u.detail = f"backed up to Drive {today}"
        return
    try:
        from lib.egon_paths import CONNECT_INDEX_DIR, FILE_EXTRACTS_DIR
    except Exception as e:
        u.ok = True
        u.detail = f"paths err {str(e)[:40]}"
        return
    if str(CONNECT_INDEX_DIR).strip("\\/").lower() == str(_DRIVE_INDEX_BACKUP).strip("\\/").lower():
        u.ok = True
        u.detail = "index is the Drive copy — backup n/a"
        return
    if not (CONNECT_INDEX_DIR / "COMPLETE.json").exists():
        u.ok = True
        u.detail = "waiting for local index"
        return
    global _index_backup_proc
    if _index_backup_proc is not None and _index_backup_proc.poll() is None:
        u.ok = True
        u.detail = "backing up to Drive… (zip)"
        return
    # The index (1.1GB vectors.npy) must NEVER be robocopy-mirrored onto Drive —
    # Drive can't reconcile a 1GB binary overwritten in place and quarantines it
    # to Lost & Found (the recurring "file not synced"). Instead write ONE dated
    # zip as a NEW file (Drive conflicts on overwrites, not new files), pruned to
    # the last 3. file_extracts stay a plain incremental mirror (small files sync
    # fine). Bruno 2026-06-30.
    backups = _DRIVE_INDEX_BACKUP.parent / "index_backups"
    code = (
        "import sys, os, glob, shutil\n"
        "sys.path.insert(0, r'{root}')\n"
        "src = r'{src}'; bdir = r'{bdir}'; day = '{day}'\n"
        "os.makedirs(bdir, exist_ok=True)\n"
        "tmp = os.path.join(r'{state}', '_index_backup_tmp')\n"
        "shutil.make_archive(tmp, 'zip', src)\n"
        "final = os.path.join(bdir, 'connect_index_' + day + '.zip')\n"
        "shutil.move(tmp + '.zip', final)\n"
        "zips = sorted(glob.glob(os.path.join(bdir, 'connect_index_*.zip')))\n"
        "[os.remove(z) for z in zips[:-3]]\n"          # rotate: keep newest 3
        "print('index backup zip:', final)\n"
    ).format(root=str(ROOT), src=str(CONNECT_INDEX_DIR), bdir=str(backups),
             day=today, state=str(ROOT / "state"))
    try:
        # extracts: small text files, incremental mirror is safe on Drive
        subprocess.Popen(
            ["robocopy", str(FILE_EXTRACTS_DIR), str(_DRIVE_EXTRACTS_BACKUP),
             "/MIR", "/R:1", "/W:1", "/NFL", "/NDL", "/NJH", "/NP"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=0x08000000)
        # index: dated zip as a new file (no in-place overwrite = no conflict)
        _index_backup_proc = subprocess.Popen(
            [str(PYW), "-c", code], cwd=str(ROOT), env=SPAWN_ENV,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008))
        _backup_private_config(today)   # tiny: config/keys/persona dated zip
        _index_backup_date = today
        u.ok = True
        u.detail = f"Drive backup (dated zip) launched {today}"
        log("info", "index_drive_backup", date=today)
    except Exception as e:
        u.ok = True
        u.detail = f"backup failed: {str(e)[:60]}"


def check_digest(u: Unit) -> None:
    """Proactivity (strategy #3): once a day, run the introspection engine and
    assemble a digest Bruno never asked for but wants — what Egon noticed,
    what the other agents did in the last 24h, substrate health. Written to
    state/daily_digest.{json,md}; the Connect widget's tray toasts when a new
    one lands. Rule-based + read-only over mind.db: no LLM, no tokens."""
    global _digest_running
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        prev = json.loads(DIGEST_JSON.read_text(encoding="utf-8")).get("date")
    except Exception:
        prev = None
    u.ok = prev == today
    u.detail = f"last={prev or 'never'}"
    allowed, why = _heavy_allowed()
    if not allowed:
        u.detail += f" ({why})"
        return
    if _digest_running or prev == today or datetime.now().hour < DIGEST_AFTER_HOUR:
        return
    _digest_running = True

    def _build():
        global _digest_running
        try:
            _generate_digest(today)
            log("info", "digest_generated", date=today)
        except Exception as e:
            log("warn", "digest_failed", error=str(e)[:200])
        finally:
            _digest_running = False

    threading.Thread(target=_build, daemon=True, name="core-digest").start()


def _generate_digest(today: str) -> None:
    import sqlite3
    # 1) fresh introspection proposals (rule-based, cheap)
    proposals = []
    try:
        from lib.mind_introspection import run_introspection
        res = run_introspection()
        proposals = res.get("proposals", res) if isinstance(res, dict) else res
    except Exception as e:
        log("warn", "introspection_failed", error=str(e)[:160])
    # 2) what the other agents did in the last 24h (durable memories)
    agent_work = []
    try:
        con = sqlite3.connect(ROOT / "state" / "mind.db", timeout=10)
        con.row_factory = sqlite3.Row
        day_ago = int(time.time()) - 86400
        rows = con.execute(
            """SELECT m.id, m.kind, substr(m.content,1,200) AS preview,
                      COALESCE(a.name,'?') AS agent
               FROM memory m LEFT JOIN agents a ON a.id = m.attribution_agent_id
               WHERE m.created_at >= ? AND m.kind IN ('decision','note','plan')
               ORDER BY m.created_at DESC LIMIT 12""", (day_ago,)).fetchall()
        agent_work = [dict(r) for r in rows]
        con.close()
    except Exception:
        pass
    # 3) substrate health snapshot
    health = {}
    try:
        health = json.loads(HEALTH.read_text(encoding="utf-8")).get("units", {})
    except Exception:
        pass

    digest = {"date": today,
              "generated": datetime.now().isoformat(timespec="seconds"),
              "proposals": proposals if isinstance(proposals, list) else [],
              "agent_work_24h": agent_work,
              "substrate": {k: v.get("ok") for k, v in health.items()}}
    DIGEST_JSON.write_text(json.dumps(digest, indent=2, ensure_ascii=False),
                           encoding="utf-8")
    # human-readable twin
    lines = [f"# Egon Daily Digest — {today}", ""]
    props = digest["proposals"]
    lines.append(f"## Egon noticed ({len(props)} insight{'s' if len(props)!=1 else ''})")
    if props:
        for p in props[:10]:
            lines.append(f"- **{p.get('title','?')}** [{p.get('severity','info')}] — "
                         f"{p.get('description','')[:260]}")
    else:
        lines.append("- Nothing flagged by introspection in the last week.")
    lines.append("")
    lines.append(f"## What your agents did in the last 24h ({len(agent_work)})")
    if agent_work:
        for w in agent_work:
            lines.append(f"- [{w['agent']}] ({w['kind']}) {w['preview'][:160]}…")
    else:
        lines.append("- No durable memories written in the last 24h.")
    lines.append("")
    ok_units = [k for k, v in digest["substrate"].items() if v]
    bad_units = [k for k, v in digest["substrate"].items() if not v]
    lines.append("## Substrate")
    lines.append(f"- healthy: {', '.join(ok_units) or '—'}")
    if bad_units:
        lines.append(f"- ⚠ down: {', '.join(bad_units)}")
    DIGEST_MD.write_text("\n".join(lines), encoding="utf-8")


def write_health(units: dict[str, Unit]) -> None:
    try:
        HEALTH.parent.mkdir(parents=True, exist_ok=True)
        HEALTH.write_text(json.dumps({
            "updated": datetime.now().isoformat(timespec="seconds"),
            "core_pid": __import__("os").getpid(),
            "units": {n: u.as_dict() for n, u in units.items()},
        }, indent=2), encoding="utf-8")
    except Exception:
        pass


# Source snapshots — the store everything downstream reads (mirrors, Connect
# index, dashboards). Was only refreshed by the 06:00 in-app pass, which never
# fired unless the Egon UI happened to be open at 6AM — kindle went stale
# 2026-05-27 and instapaper (3,212 harvested items) NEVER got a snapshot.
# Bruno 2026-06-12: the always-on core owns freshness now. Daily, off-thread.

def check_snapshots(u: Unit) -> None:
    from lib import snapshots_runner
    
    last = 0.0
    if snapshots_runner.SNAP_MARK.exists():
        try:
            last = float(json.loads(snapshots_runner.SNAP_MARK.read_text(encoding="utf-8"))["ts"])
        except Exception:
            pass
            
    age_h = (time.time() - last) / 3600 if last else None
    u.ok = age_h is not None and age_h < 48
    
    is_running = snapshots_runner._running
    u.detail = (f"all-sources age={age_h:.0f}h" if age_h is not None
                else "never") + (" (refreshing)" if is_running else "")
                
    allowed, why = snapshots_runner.is_heavy_allowed(caller="core")
    if not allowed:
        u.detail += f" ({why})"
        
    snapshots_runner.run_snapshots_if_due(force=False, caller="core")



# Notion mirror increment cadence — every 5 min advance one bounded batch.
# Obsidian is fully mirrored by the index cycle (cheap local writes); Notion
# fills slowly to respect its API. Bruno 2026-06-12.
MIRROR_EVERY_S = int(os.environ.get("EGON_CORE_MIRROR_EVERY_S", "3600"))
MIRROR_BATCH = int(os.environ.get("EGON_CORE_MIRROR_BATCH", "25"))
NOTION_BODY_BATCH = int(os.environ.get("EGON_CORE_NOTION_BODY_BATCH", "5"))
_mirror_last = 0.0
_mirror_proc = None       # isolated Notion-push subprocess


def check_mirror(u: Unit) -> None:
    global _mirror_last, _mirror_proc
    state_file = ROOT / "state" / "mirror_runner.json"
    catchup_file = ROOT / "state" / "notion_catchup_active.json"

    # Read catchup state
    catchup_active = False
    try:
        if catchup_file.exists():
            import json as _json
            catchup_active = _json.loads(catchup_file.read_text(encoding="utf-8")).get("active", False)
    except Exception:
        pass

    try:
        import json as _json
        cur = _json.loads(state_file.read_text(encoding="utf-8")).get(
            "notion_cursor", {}) if state_file.exists() else {}
        u.ok = True
        u.detail = ("catchup active" if catchup_active else "notion") + ": " + ", ".join(
            f"{k}={v}" for k, v in list(cur.items())[:4]) if cur else "idle"
    except Exception:
        u.ok = True
        u.detail = "idle"
    allowed, why = _heavy_allowed()
    if not allowed and not catchup_active:
        u.detail += f" ({why})"
        return
    if _mirror_proc is not None and _mirror_proc.poll() is None:
        u.detail += " (pushing, subprocess)"
        return
    if _any_heavy_running():
        u.detail += " (queued: heavy job running)"
        return
    if not catchup_active and time.time() - _mirror_last < MIRROR_EVERY_S:
        return
    _mirror_last = time.time()
    # Run the Notion push in an ISOLATED subprocess. In-thread it held the 258k
    # Zotero snapshot cache resident in this always-on supervisor (egon_core was
    # ~700MB); a subprocess frees it on exit and keeps the supervisor tiny.
    # Bruno 2026-07-01.
    code = (
        "import sys, json, time\n"
        "sys.path.insert(0, r'{root}')\n"
        "from pathlib import Path\n"
        "cf = Path(r'{root}') / 'state' / 'notion_catchup_active.json'\n"
        "def cu():\n"
        "    try: return json.loads(cf.read_text(encoding='utf-8')).get('active', False)\n"
        "    except Exception: return False\n"
        "from lib import mirror_runner\n"
        "if not cu():\n"
        "    try:\n"
        "        from lib import notion_body; notion_body.refresh(batch={nbb})\n"
        "    except Exception: pass\n"
        "if cu():\n"
        "    while True:\n"
        "        r = mirror_runner.run_notion_increment(batch=200)\n"
        "        if r.get('pushed', 0) == 0:\n"
        "            cf.write_text(json.dumps({{'active': False}}), encoding='utf-8'); break\n"
        "        if not cu(): break\n"
        "        time.sleep(2)\n"
        "else:\n"
        "    print(mirror_runner.run_notion_increment(batch={mb}))\n"
    ).format(root=str(ROOT), nbb=NOTION_BODY_BATCH, mb=MIRROR_BATCH)
    try:
        _mirror_proc = subprocess.Popen(
            [str(PYW), "-c", code], cwd=str(ROOT), env=SPAWN_ENV,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008))
        u.detail += " (push launched, subprocess)"
    except Exception as e:
        u.detail += f" (launch failed: {str(e)[:40]})"


def main() -> int:
    # FAIL-SAFE single-instance guard: if the guard cannot run, EXIT rather than
    # proceed unguarded — two unguarded cores = two adb keepalive loops = the PC
    # crash on Chrome-open (2026-06-15 & -17). Never start unless provably alone.
    try:
        from lib.single_instance_mutex import claim_or_exit
        alone = claim_or_exit("Egon-Core-2026-06")
    except Exception as e:
        log("error", "core_guard_failed_exit", error=str(e)[:160])
        return 0
    if not alone:
        log("info", "core_already_running_exit")
        return 0

    log("info", "core_start")

    # Always-on phone keepalive: keep the wireless-debug link alive AND self-heal
    # the Connect "Capture" accessibility grant (wiped on every APK reinstall),
    # even when the desktop app is closed. The service guards itself with a
    # cross-process mutex, so if the desktop app is also open only ONE adb loop
    # runs (two loops crashed the PC — see single-instance note above). Bruno 2026-06-24.
    try:
        from egon_app.services.phone_keepalive_service import PhoneKeepaliveService
        _keepalive = PhoneKeepaliveService()
        _keepalive.start()
        log("info", "phone_keepalive_started_incore")
    except Exception as e:
        log("warn", "phone_keepalive_start_failed", error=str(e)[:160])

    units = {"mind": Unit("mind"), "mobile_connect": Unit("mobile_connect"),
             "headroom": Unit("headroom"),
             "ollama": Unit("ollama"),
             "connect_index": Unit("connect_index"),
             "hydration": Unit("hydration"),
             "reembed": Unit("reembed"),
             "hermes": Unit("hermes"),
             "concept_graph": Unit("concept_graph"),
             "index_backup": Unit("index_backup"),
             "daily_digest": Unit("daily_digest"),
             "snapshots": Unit("snapshots"),
             "mirror": Unit("mirror"),
             "canonical": Unit("canonical"),
             "exhaustive": Unit("exhaustive")}
    _reap_heavy("startup-orphans")   # clear any heavy jobs a prior instance left
    while True:
        try:
            # HARD RULE: the moment the user is active, kill ALL heavy work so it
            # can never thrash the machine while Bruno uses it.
            if _idle_seconds() < HEAVY_REAP_IDLE_S:
                _reap_heavy("user-active")
            check_mind(units["mind"])
            check_mobile_connect(units["mobile_connect"])
            check_headroom(units["headroom"])
            check_ollama(units["ollama"])
            check_index(units["connect_index"])
            check_hydration(units["hydration"])
            check_reembed(units["reembed"])
            check_hermes(units["hermes"])
            check_concept_graph(units["concept_graph"])
            check_index_backup(units["index_backup"])
            check_digest(units["daily_digest"])
            check_snapshots(units["snapshots"])
            check_mirror(units["mirror"])
            check_canonical(units["canonical"])
            check_exhaustive(units["exhaustive"])
            write_health(units)
        except Exception as e:
            log("error", "core_cycle_error", error=str(e)[:200])
        # Interruptible sleep: check every 1s so heavy work is reaped within ~1s
        # of the user returning, not up to a full cycle later.
        for _ in range(CHECK_EVERY_S):
            time.sleep(1)
            if _idle_seconds() < HEAVY_REAP_IDLE_S and _any_heavy_running():
                _reap_heavy("user-active")


if __name__ == "__main__":
    raise SystemExit(main())
