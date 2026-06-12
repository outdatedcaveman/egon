"""Egon agent pass runner.

Invokes Claude Code (`claude -p`) with prompts/daily_pass.md, archives the previous
last_pass.json to history/, and writes the new one atomically into the vault.

Usage:
    python scripts/pass.py              # full daily pass
    python scripts/pass.py --kind inbox # inbox-only sub-pass
    python scripts/pass.py --kind mirror# trigger nightly mirror only
    python scripts/pass.py --dry-run    # don't call claude, just smoke-test the plumbing
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lib.silent_subprocess  # noqa: F401  — suppress console windows on Windows

import argparse
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Prevent indefinite socket hangs in third-party APIs (e.g. Google APIs, iTunes)
socket.setdefaulttimeout(45.0)

ROOT = Path(__file__).resolve().parent.parent
from lib.egon_paths import VAULT_STATE
LAST_PASS = VAULT_STATE / "last_pass.json"
HISTORY = VAULT_STATE / "history"
LOG_DIR = ROOT / "logs"

PROMPTS = {
    "daily":  ROOT / "prompts" / "daily_pass.md",
    "inbox":  ROOT / "prompts" / "daily_pass.md",   # placeholder; will be inbox_only.md in P3
    "mirror": None,                                 # not a claude call — see _run_mirror
    "snapshots": None,                              # snapshot all configured adapters — see _run_snapshots
}

# adapters that can run unattended each night (read-only, no auth prompt)
# `instapaper` removed 2026-05-20 — it has no snapshot() method (stub only).
# Added youtube_music + notion_workspace (broke "Media" / Notion views).
# chrome_tabs is intentionally NOT here: it needs Chrome's :9222 remote-debug
# port live on desktop, which we don't keep on routinely.
SNAPSHOT_ADAPTERS = (
    ("chrome_bookmarks", "lib.adapters.chrome_bookmarks"),
    ("zotero",           "lib.adapters.zotero_local"),
    ("letterboxd",       "lib.adapters.letterboxd"),
    ("youtube_music",    "lib.adapters.youtube"),
    ("notion_workspace", "lib.adapters.notion_workspace"),
    ("tvtime",           "lib.adapters.tvtime"),
    ("kindle",           "lib.adapters.kindle"),
    ("pocketcasts",      "lib.adapters.pocketcasts"),
    ("paperpile",        "lib.adapters.paperpile"),
    ("instapaper",       "lib.adapters.instapaper"),
    ("youtube_history",  "lib.adapters.youtube_history"),
)


def _run_snapshots() -> None:
    """Pull a fresh snapshot from every adapter, then mirror to vault.

    Each adapter step has a hard time budget so a slow mirror (e.g. writing
    87k Chrome-bookmarks markdown files to Google Drive) can't block the
    other adapters from running. The snapshot JSON itself is always written
    by `write_snapshot` — that's what the dashboard views read from. The
    slower vault-mirror is best-effort.
    """
    from importlib import import_module
    import threading
    sys.path.insert(0, str(ROOT))
    from lib.snapshot_store import write_snapshot   # noqa: E402
    from lib.mirror import mirror_snapshot          # noqa: E402

    # Mirror = vault MD + Notion sync. It's SLOW for big sources (chrome_bookmarks
    # writes tens of thousands of files to Google Drive). The snapshot JSON
    # (which dashboard views read) is written by write_snapshot — that's the
    # critical path. Mirror is FIRE-AND-FORGET in a daemon thread; the snapshot
    # loop never waits for it. Daemons keep running in the background and exit
    # when the script ends.
    def _mirror_bg(sid, sn):
        try:
            m = mirror_snapshot(sid, sn) or {}
            log.info("mirror %s · status=%s · vault=%d, notion=%d, errors=%d",
                     sid, m.get("status", "?"), m.get("written_vault", 0),
                     m.get("written_notion", 0), len(m.get("errors", [])))
        except Exception as e:
            log.warning("mirror %s failed: %s", sid, e)

    mirror_threads = []
    for source_id, mod_name in SNAPSHOT_ADAPTERS:
        try:
            mod = import_module(mod_name)
            
            snap = None
            snap_err = None
            
            def run_snap():
                nonlocal snap, snap_err
                try:
                    snap = mod.snapshot()
                except Exception as e:
                    snap_err = e
            
            t_snap = threading.Thread(target=run_snap, name=f"snap-{source_id}", daemon=True)
            t_snap.start()
            t_snap.join(timeout=45.0)
            
            if t_snap.is_alive():
                log.warning("snapshot %s timed out after 45 seconds", source_id)
                continue
            if snap_err:
                log.warning("snapshot %s raised exception: %s", source_id, snap_err)
                continue
            if snap is None:
                log.warning("snapshot %s returned None", source_id)
                continue

            if snap.get("status") != "ok":
                log.warning("snapshot %s skipped: %s",
                            source_id, snap.get("error", snap.get("status")))
                continue
            local, vault = write_snapshot(source_id, snap)
            log.info("snapshot %s OK · %d items · local=%s vault=%s",
                     source_id, snap.get("count", 0), local.name, vault is not None)

            t = threading.Thread(target=_mirror_bg, args=(source_id, snap),
                                 daemon=True, name=f"mirror-{source_id}")
            t.start()
            mirror_threads.append(t)
        except Exception as e:
            log.warning("snapshot %s failed: %s", source_id, e)

    # Brief grace period for fast mirrors (youtube_music, notion_workspace etc.
    # have no_mapper and return instantly). Don't wait for the slow ones.
    grace = 15
    for t in mirror_threads:
        t.join(timeout=max(0.1, grace / len(mirror_threads)))
    log.info("snapshot pass: %d adapters processed, mirrors continuing in background",
             len(mirror_threads))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("egon.pass")


def _archive_previous() -> None:
    if not LAST_PASS.exists():
        return
    HISTORY.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    dest = HISTORY / f"{today}.json"
    if dest.exists():
        # already archived today; rotate with timestamp
        ts = datetime.now().strftime("%H%M%S")
        dest = HISTORY / f"{today}-{ts}.json"
    shutil.copy2(LAST_PASS, dest)
    log.info("archived previous pass → %s", dest.name)


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _claude_cmd() -> str:
    """Locate the claude CLI. Override with $CLAUDE_BIN if needed."""
    env = os.environ.get("CLAUDE_BIN")
    if env and Path(env).exists():
        return env
    for cand in ("claude", "claude.cmd", "claude.exe"):
        found = shutil.which(cand)
        if found:
            return found
    raise RuntimeError("claude CLI not found on PATH (set $CLAUDE_BIN)")


def _run_claude_pass(kind: str, dry_run: bool) -> dict | None:
    prompt_path = PROMPTS[kind]
    if not prompt_path or not prompt_path.exists():
        raise RuntimeError(f"prompt not found: {prompt_path}")
    prompt = prompt_path.read_text(encoding="utf-8")

    if dry_run:
        log.info("[dry-run] would call: claude -p (length=%d)", len(prompt))
        return None

    bin_ = _claude_cmd()
    log.info("invoking %s with %s prompt …", bin_, kind)
    res = subprocess.run(
        [bin_, "-p", prompt, "--output-format", "json"],
        capture_output=True, text=True, encoding="utf-8", timeout=20 * 60,
    )
    if res.returncode != 0:
        raise RuntimeError(f"claude -p failed (rc={res.returncode}): {res.stderr[:500]}")

    # claude -p --output-format json returns metadata + result. The result must itself be JSON
    # matching last_pass.json schema (per daily_pass.md).
    meta = json.loads(res.stdout)
    result_text = meta.get("result", "").strip()
    if not result_text:
        raise RuntimeError("claude returned empty result")
    try:
        return json.loads(result_text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"claude result was not valid JSON: {e}; first 200 chars: {result_text[:200]}")


def _run_mirror() -> None:
    """Trigger the bidirectional Notion ↔ Obsidian mirror.

    Order matters: write-back FIRST (push local edits to Notion), then forward
    pull (refresh vault from Notion). This way any vault edits make it to Notion
    before Notion's last_edited_time gets re-stamped by the forward pass.

    Write-back is only run when WRITE_BACK_ENABLED=1 in claude-meta/.env, so
    users opt in once they trust the round-trip.
    """
    meta_scripts = Path(os.environ.get("CLAUDE_META_SCRIPTS", str(Path.home() / "claude-meta" / "scripts")))
    fwd = meta_scripts / "notion_to_obsidian_mirror.py"
    rev = meta_scripts / "obsidian_to_notion_writeback.py"
    if not fwd.exists():
        raise RuntimeError(f"mirror script not found: {fwd}")

    # Read WRITE_BACK_ENABLED from claude-meta/.env (don't require it in our process env)
    from lib.egon_paths import ENV_FILE as env_path
    writeback_on = False
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("WRITE_BACK_ENABLED=1"):
                writeback_on = True
                break

    if writeback_on and rev.exists():
        log.info("running obsidian→notion write-back (vault edits → Notion) …")
        res = subprocess.run([sys.executable, str(rev), "--commit"], timeout=30 * 60)
        log.info("write-back exit code: %d", res.returncode)
    else:
        log.info("write-back skipped (WRITE_BACK_ENABLED not set in claude-meta/.env)")

    log.info("running notion→vault mirror (Notion → vault) …")
    res = subprocess.run([sys.executable, str(fwd), "--commit"], timeout=30 * 60)
    log.info("forward mirror exit code: %d", res.returncode)

    # Restore-point snapshot — runs LAST so it captures the freshly-mirrored state.
    backup_script = meta_scripts / "mirror_backup.py"
    if backup_script.exists():
        log.info("taking parallel restore-point backup (local + Drive) …")
        res = subprocess.run([sys.executable, str(backup_script)], timeout=15 * 60)
        log.info("backup exit code: %d", res.returncode)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", choices=("daily", "inbox", "mirror", "snapshots"), default="daily")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    LOG_DIR.mkdir(exist_ok=True)
    file_log = logging.FileHandler(LOG_DIR / f"pass-{datetime.now():%Y-%m}.log", encoding="utf-8")
    file_log.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logging.getLogger().addHandler(file_log)

    log.info("=== pass start · kind=%s · dry-run=%s ===", args.kind, args.dry_run)

    try:
        if args.kind == "mirror":
            _run_mirror()
            return 0
        if args.kind == "snapshots":
            _run_snapshots()
            return 0
        # daily pass also pulls snapshots so all the dashboards stay fresh
        if args.kind == "daily":
            _run_snapshots()
            try:
                from scripts.sync_notion_progress import main as _sync_notion
                _sync_notion()
            except Exception as e:
                log.warning("notion sync failed: %s", e)

        payload = _run_claude_pass(args.kind, args.dry_run)
        if payload is None:
            log.info("[dry-run] skipping vault write")
            return 0

        _archive_previous()
        _atomic_write(LAST_PASS, payload)
        log.info("wrote %s", LAST_PASS)
        return 0

    except Exception as e:
        log.error("pass failed: %s", e)
        return 1
    finally:
        log.info("=== pass end ===")


if __name__ == "__main__":
    raise SystemExit(main())
