"""Clean rebuild of state/mind.db from scratch with the current resolver.

WHY: the mind ingester is idempotent (skips already-seen sessions via
state/mind_ingest_state.json). When the project resolver improves, already
-ingested sessions keep their OLD (often garbage / None) attribution. The
only way to re-attribute everything is to wipe the DB + ingest state and
re-ingest from zero. Bruno 2026-05-29.

SAFE: backs up mind.db and the ingest state before touching anything
(never delete without a backup — Bruno's standing rule).

SELF-CONTAINED: starts Panop's FastAPI in-process (the ingester writes via
the local :8000 REST API), runs ingest passes with caps lifted until the
project/session counts stop growing, prints the resulting project list,
then shuts the server down. Run with Egon CLOSED so :8000 / the DB file
aren't held by the live app.

    .venv\\Scripts\\pythonw.exe scripts\\rebuild_mind.py
    (output -> logs/_rebuild_mind.out)
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# Must come before anything that may spawn adb/git/etc. so no cmd windows
# flash on Bruno's desktop while this runs Panop in-process. Bruno 2026-05-29.
import lib.no_console  # noqa: E402,F401  (side-effect: patches subprocess)
OUT = ROOT / "logs" / "_rebuild_mind.out"
DB = ROOT / "state" / "mind.db"
STATE = ROOT / "state" / "mind_ingest_state.json"


def log(msg: str) -> None:
    with OUT.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


def main() -> int:
    OUT.write_text("", encoding="utf-8")

    # 1) back up everything we're about to wipe (with a stable suffix).
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
    backups = ROOT / ".backups"
    backups.mkdir(exist_ok=True)
    for src in (DB, STATE):
        if src.exists():
            dst = backups / f"{src.name}.bak-rebuild-{stamp}"
            shutil.copy2(src, dst)
            log(f"backed up {src.name} -> {dst.name}")
    # WAL sidecars too, so the backup is consistent.
    for ext in ("-wal", "-shm"):
        p = DB.with_name(DB.name + ext)
        if p.exists():
            shutil.copy2(p, backups / f"{p.name}.bak-rebuild-{stamp}")

    # 2) wipe the DB + ingest state for a from-zero rebuild.
    for ext in ("", "-wal", "-shm"):
        p = DB.with_name(DB.name + ext)
        if p.exists():
            p.unlink()
            log(f"removed {p.name}")
    if STATE.exists():
        STATE.unlink()
        log("removed mind_ingest_state.json")

    # 3) lift the ingest caps so the rebuild covers ALL history, not just the
    #    first 30 sessions / 200 events. Done by monkeypatching the module
    #    constants before importing the scan functions use them.
    import lib.mind_ingest as mi
    mi.MAX_NEW_SESSIONS_PER_PASS = 100000
    mi.MAX_EVENTS_PER_SESSION = 400  # plenty for attribution; keeps it bounded
    log(f"caps lifted: sessions/pass={mi.MAX_NEW_SESSIONS_PER_PASS} "
        f"events/session={mi.MAX_EVENTS_PER_SESSION}")

    # 4) start Panop in-process so the ingester's REST writes land on :8000.
    import uvicorn
    from external.panop_server.main import app as panop_app
    cfg = uvicorn.Config(panop_app, host="127.0.0.1", port=8000,
                         log_config=None, log_level="warning",
                         access_log=False, lifespan="on")
    server = uvicorn.Server(cfg)
    th = threading.Thread(target=server.run, name="panop", daemon=True)
    th.start()
    # wait for bind
    import socket
    for _ in range(40):
        try:
            with socket.create_connection(("127.0.0.1", 8000), timeout=0.5):
                break
        except Exception:
            time.sleep(0.3)
    else:
        log("ERROR: panop never bound :8000")
        return 1
    log("panop up on :8000")

    # 5) ingest passes until counts stabilise.
    prev = -1
    for i in range(8):
        counts = mi.ingest_once()
        try:
            con = sqlite3.connect(DB)
            nproj = con.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
            nsess = con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            nact = con.execute("SELECT COUNT(*) FROM activity").fetchone()[0]
            con.close()
        except Exception as e:
            nproj = nsess = nact = -1
            log(f"count err: {e}")
        log(f"pass {i+1}: {counts} | projects={nproj} sessions={nsess} activity={nact}")
        if nsess == prev:
            break
        prev = nsess

    # 6) report the final project list.
    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT p.slug, COUNT(s.id) AS sessions FROM projects p "
        "LEFT JOIN sessions s ON s.project_id = p.id "
        "GROUP BY p.id ORDER BY sessions DESC").fetchall()
    log("\n=== FINAL PROJECTS ===")
    for r in rows:
        log(f"  {r['slug']:30s} sessions={r['sessions']}")
    # unattributed sessions
    n_un = con.execute(
        "SELECT COUNT(*) FROM sessions WHERE project_id IS NULL").fetchone()[0]
    log(f"  (unattributed sessions: {n_un})")
    con.close()

    server.should_exit = True
    time.sleep(1)
    log("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
