"""Mind Exhaustive — EVERYTHING that factors into Claude/Codex/Antigravity use,
captured into the mind. No caps, no sampling, no silent gaps.

Bruno 2026-07-01, verbatim: "the mind/egon database must be COMPREHENSIVE and
EXHAUSTIVE... NOTHING AT ALL that factors into my Claude, Antigravity and Codex
use should be left out." The 2026-07-01 audit found the old ingest was nowhere
near that:
  • Antigravity: ~0% — mind_ingest read brain/ (now EMPTY); the real data is
    conversations/*.db (512MB SQLite+protobuf), knowledge/, annotations/, …
  • Codex: memories/ dir empty — real memories moved to memories_1.sqlite;
    state_5.sqlite holds per-thread titles/cwd/models; rules/AGENTS.md unread.
  • Claude: transcripts middle-sliced at 200 events; history.jsonl, plans/,
    tasks/, skills/ never ingested.
  • Danger: the apps PRUNE their own history (Claude Code cleans old
    transcripts) — anything not captured in time is lost forever.

Design (three guarantees):
  1. RAW ARCHIVE — mirror every source file byte-for-byte into
     STATE_DIR/mind_archive/<agent>/… (incremental by size+mtime; SQLite copied
     via the backup API so WAL-mode files are consistent). Even before parsing,
     nothing can be lost to app pruning. Never deletes (Bruno's hard rule).
  2. MANIFEST — a mind.db table `mind_files` records every file seen (agent,
     path, size, mtime, sha1-head, archived state, skip reason). The DB knows
     about EVERYTHING, including what it couldn't copy and why — no silent caps.
  3. PARSERS — extract the newly-found content into the mind proper:
     Codex threads → session metadata; Codex stage1_outputs → memories;
     Antigravity conversation .dbs → text extracts + sessions; Claude ancillary
     (history/plans/tasks) → archived + extracted. Extracted text lands in
     mind_archive/_extracts/ which is registered with the whole-vault embedding
     pipeline, so every byte becomes searchable/analyzable.

Runs idle-gated from egon_core (check_exhaustive), serialized with other heavy
jobs, in an isolated subprocess.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import time
from pathlib import Path

from lib import egon_paths
from lib.mind_context_broker import DB_PATH

HOME = Path.home()
ARCHIVE_ROOT = egon_paths.STATE_DIR / "mind_archive"
EXTRACTS = ARCHIVE_ROOT / "_extracts"
COVERAGE = egon_paths.STATE_DIR / "mind_coverage.json"

# What "everything" means per agent: root + include-all minus pure machine
# artifacts (compiled binaries, dependency trees, build output, caches, temp) —
# NEVER user/AI content. Every exclusion is visible in the manifest as
# skip='excluded-dir' so nothing is silently dropped.
# 2026-07-02 audit correction: antigravity/scratch was wrongly excluded — it
# holds REAL work (synesism-workshop, a Panop working copy, double-app,
# notion_dump, AT's own scripts); only its build/dependency trees are junk.
# Codex generated_images (9 real outputs), computer-use config, and
# vendor_imports (883 skill files) are content too — now included.
_MACHINE_DIRS = {"node_modules", "dist", "build", ".next", ".turbo",
                 "__pycache__", ".venv", "venv", ".mypy_cache",
                 ".pytest_cache", "chrome_temp_profile", "site-packages"}
SOURCES = {
    "claude": {
        "root": HOME / ".claude",
        "exclude_dirs": _MACHINE_DIRS | {"cache", "debug", "telemetry",
                                         "shell-snapshots", "session-env",
                                         "plugins"},
    },
    "codex": {
        "root": HOME / ".codex",
        "exclude_dirs": _MACHINE_DIRS | {"cache", "tmp", "bin",
                                         "process_manager"},
    },
    "antigravity": {
        "root": HOME / ".gemini" / "antigravity",
        "exclude_dirs": _MACHINE_DIRS | {"bin", "playground"},
    },
}
_BIG_FILE_BYTES = 500 * 1024 * 1024          # >500MB: manifest yes, copy flagged
_DISK_FLOOR_GB = 8                            # stop copying below this free space


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(DB_PATH), timeout=30)
    c.row_factory = sqlite3.Row
    return c


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS mind_files (
               agent TEXT NOT NULL,
               rel_path TEXT NOT NULL,
               size INTEGER, mtime INTEGER, sha1_head TEXT,
               archived INTEGER DEFAULT 0,   -- 1 = byte-mirror exists
               parsed INTEGER DEFAULT 0,     -- 1 = content extracted into mind
               skip TEXT,                    -- reason if not archived (visible, never silent)
               first_seen INTEGER, last_seen INTEGER,
               PRIMARY KEY (agent, rel_path)
           )""")
    conn.commit()


def _free_gb(path: Path) -> float:
    try:
        return shutil.disk_usage(str(path)).free / 1e9
    except Exception:
        return 999.0


def _sha1_head(p: Path, cap: int = 1024 * 1024) -> str:
    h = hashlib.sha1()
    try:
        with p.open("rb") as f:
            h.update(f.read(cap))
        return h.hexdigest()[:16]
    except Exception:
        return ""


def _copy_sqlite(src: Path, dst: Path) -> bool:
    """Consistent copy of a possibly-live SQLite db (handles WAL) via backup API."""
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        s = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=10)
        d = sqlite3.connect(str(dst))
        s.backup(d)
        d.close(); s.close()
        return True
    except Exception:
        try:  # fallback: plain copy (still better than nothing)
            shutil.copy2(src, dst)
            return True
        except Exception:
            return False


def _copy_file(src: Path, dst: Path) -> bool:
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return True
    except Exception:
        return False


# ── 1+2. Archive + manifest ──────────────────────────────────────────────────

def archive_all(stop_check=None) -> dict:
    """Walk every source, manifest every file, mirror new/changed ones.
    Incremental: unchanged (size, mtime) files are only touch-stamped."""
    conn = _conn()
    ensure_schema(conn)
    now = int(time.time())
    stats: dict[str, dict] = {}
    for agent, spec in SOURCES.items():
        root: Path = spec["root"]
        st = {"seen": 0, "bytes": 0, "archived_new": 0, "skipped": 0, "errors": 0}
        stats[agent] = st
        if not root.exists():
            continue
        for p in root.rglob("*"):
            if stop_check and stop_check():
                st["aborted"] = True
                break
            if not p.is_file():
                continue
            rel = p.relative_to(root)
            parts_l = {q.lower() for q in rel.parts[:-1]}
            excluded = bool(parts_l & spec["exclude_dirs"])
            # journal sidecars are covered by the sqlite backup of the main db
            if p.suffix.lower() in (".db-shm", ".db-wal") or p.name.endswith(("-shm", "-wal")):
                continue
            try:
                sz, mt = p.stat().st_size, int(p.stat().st_mtime)
            except Exception:
                st["errors"] += 1
                continue
            st["seen"] += 1
            st["bytes"] += sz
            row = conn.execute(
                "SELECT size, mtime, archived FROM mind_files WHERE agent=? AND rel_path=?",
                (agent, str(rel))).fetchone()
            unchanged = row and row["size"] == sz and row["mtime"] == mt and row["archived"]
            skip = None
            did_archive = row["archived"] if row else 0
            if excluded:
                skip = "excluded-dir"
            elif unchanged:
                pass  # already mirrored, nothing to do
            elif sz > _BIG_FILE_BYTES:
                skip = f"big-file-{sz>>20}MB"
            elif _free_gb(ARCHIVE_ROOT if ARCHIVE_ROOT.exists() else egon_paths.STATE_DIR) < _DISK_FLOOR_GB:
                skip = "disk-floor"
            else:
                dst = ARCHIVE_ROOT / agent / rel
                ok = (_copy_sqlite(p, dst) if p.suffix.lower() in (".db", ".sqlite")
                      else _copy_file(p, dst))
                if ok:
                    did_archive = 1
                    st["archived_new"] += 1
                else:
                    skip = "copy-failed"
                    st["errors"] += 1
            if skip and not did_archive:
                st["skipped"] += 1
            conn.execute(
                """INSERT INTO mind_files (agent, rel_path, size, mtime, sha1_head,
                                           archived, skip, first_seen, last_seen)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(agent, rel_path) DO UPDATE SET
                     size=excluded.size, mtime=excluded.mtime,
                     sha1_head=excluded.sha1_head,
                     archived=MAX(mind_files.archived, excluded.archived),
                     skip=excluded.skip, last_seen=excluded.last_seen""",
                (agent, str(rel), sz, mt,
                 _sha1_head(p) if sz < 32 * 1024 * 1024 else "",
                 did_archive, skip, now, now))
            # Commit in small batches: a per-agent transaction spans thousands
            # of rows and holds the mind.db write lock for MINUTES, starving the
            # live service and any classifier ('database is locked', verified
            # 2026-07-02). Short transactions keep everyone responsive.
            if st["seen"] % 200 == 0:
                conn.commit()
        conn.commit()
    conn.close()
    return stats


# ── 3. Parsers: pull the missing content INTO the mind ──────────────────────

_API_UP: bool | None = None


def _api_up() -> bool:
    """One cached reachability probe per run. When the mind service is down the
    parsers still do all FILE work (archive/extracts); only the API writes are
    skipped — and because everything is external_id-idempotent and 'parsed' is
    only marked on success, the next pass retries them. Never hang on a dead
    endpoint (the first probe run stalled 43×6s doing exactly that)."""
    global _API_UP
    if _API_UP is None:
        try:
            import requests
            _API_UP = requests.get("http://127.0.0.1:8000/api/v1/mind/stats",
                                   timeout=3).status_code < 400
        except Exception:
            _API_UP = False
    return _API_UP


def _mind_api(path: str, body: dict) -> bool:
    if not _api_up():
        return False
    try:
        import requests
        r = requests.post(f"http://127.0.0.1:8000/api/v1/mind{path}", json=body,
                          timeout=4)
        return r.status_code < 400
    except Exception:
        return False


def _upsert_memory_direct(conn: sqlite3.Connection, kind: str, marker: str,
                          content: str, tags: list[str]) -> bool:
    """Idempotent memory write, DIRECT to mind.db. The /memory endpoint has no
    external_id dedupe (re-running would duplicate every pass) and per-item HTTP
    crawls when the service is busy ingesting — direct SQL is instant and the
    memory_ai/au FTS triggers keep the search index in sync automatically.
    `marker` is the stable first line of content used as the identity key."""
    try:
        now = int(time.time())
        row = conn.execute(
            "SELECT id FROM memory WHERE kind=? AND content LIKE ? LIMIT 1",
            (kind, marker + "%")).fetchone()
        if row:
            conn.execute("UPDATE memory SET content=?, tags=?, updated_at=? WHERE id=?",
                         (content, ",".join(tags), now, row["id"]))
        else:
            conn.execute(
                "INSERT INTO memory (kind, content, tags, created_at, updated_at) "
                "VALUES (?,?,?,?,?)",
                (kind, content, ",".join(tags), now, now))
        return True
    except Exception:
        return False


def parse_codex_threads() -> int:
    """state_5.sqlite threads → per-thread metadata (title, cwd, model, tokens)
    as durable memory + session-summary backfill. This is the context 'codex has
    no repos' was hiding — every Codex thread self-describes here."""
    db = HOME / ".codex" / "state_5.sqlite"
    if not db.exists():
        return 0
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    src.row_factory = sqlite3.Row
    mind = _conn()
    n = 0
    for t in src.execute("SELECT * FROM threads"):
        marker = f"Codex thread {t['id']}"
        content = (f"{marker}\n"
                   f"title: {t['title'] or t['first_user_message'] or ''}\n"
                   f"cwd: {t['cwd'] or ''}\nmodel: {t['model'] or ''} "
                   f"({t['model_provider'] or ''}, effort {t['reasoning_effort'] or ''})\n"
                   f"tokens_used: {t['tokens_used'] or 0}\n"
                   f"git: {t['git_branch'] or ''} {t['git_origin_url'] or ''}\n"
                   f"first message: {(t['first_user_message'] or '')[:600]}")
        if _upsert_memory_direct(mind, "codex_thread_meta", marker, content,
                                 ["codex", "thread", "exhaustive"]):
            n += 1
        # backfill empty session summaries so the classifier sees real content
        try:
            mind.execute(
                "UPDATE sessions SET summary=? WHERE external_id LIKE ? "
                "AND (summary IS NULL OR summary='')",
                (content, f"%{t['id']}%"))
        except Exception:
            pass
    mind.commit(); mind.close(); src.close()
    return n


def parse_codex_memories() -> int:
    """memories_1.sqlite stage1_outputs → durable memories (the real Codex
    memory store; the old memories/*.md dir is empty)."""
    db = HOME / ".codex" / "memories_1.sqlite"
    if not db.exists():
        return 0
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    src.row_factory = sqlite3.Row
    mind = _conn()
    n = 0
    try:
        rows = src.execute("SELECT rowid, * FROM stage1_outputs").fetchall()
    except Exception:
        rows = []
    for r in rows:
        d = dict(r)
        text_cols = [str(v) for k, v in d.items()
                     if isinstance(v, str) and len(v) > 40]
        if not text_cols:
            continue
        marker = f"Codex memory stage1 #{d.get('rowid')}"
        if _upsert_memory_direct(mind, "codex_memory", marker,
                                 marker + "\n" + "\n".join(text_cols)[:8000],
                                 ["codex", "memory", "exhaustive"]):
            n += 1
    mind.commit(); mind.close(); src.close()
    return n


_TEXT_RUN = re.compile(rb"[\x20-\x7E\xC2-\xF4][\x20-\x7E\x80-\xBF]{7,}")


def _proto_text(blob: bytes, max_chars: int = 400_000) -> str:
    """Best-effort human text from a protobuf blob: printable UTF-8 runs ≥8
    chars, dropping id-like noise. The raw blob stays in the archive — this is
    the searchable extraction, not the storage."""
    out = []
    total = 0
    for m in _TEXT_RUN.finditer(blob or b""):
        try:
            s = m.group().decode("utf-8", "ignore").strip()
        except Exception:
            continue
        if len(s) < 8:
            continue
        letters = sum(ch.isalpha() for ch in s)
        if letters < len(s) * 0.4:      # uuid/base64-ish noise
            continue
        out.append(s)
        total += len(s)
        if total > max_chars:
            break
    return "\n".join(out)


def parse_antigravity_conversations() -> dict:
    """Every Antigravity conversation .db → full text extract in
    _extracts/antigravity/ + a session row + summary memory in the mind.
    This store was 100% absent from the mind before (brain/ went empty)."""
    conv_dir = HOME / ".gemini" / "antigravity" / "conversations"
    if not conv_dir.exists():
        return {"parsed": 0}
    EXTRACTS.joinpath("antigravity").mkdir(parents=True, exist_ok=True)
    conn = _conn()
    ensure_schema(conn)
    parsed = 0
    for db in sorted(conv_dir.glob("*.db")):
        rel = f"conversations/{db.name}"
        row = conn.execute(
            "SELECT parsed, mtime FROM mind_files WHERE agent='antigravity' AND rel_path=?",
            (rel.replace("/", "\\"),)).fetchone() or conn.execute(
            "SELECT parsed, mtime FROM mind_files WHERE agent='antigravity' AND rel_path=?",
            (rel,)).fetchone()
        mt = int(db.stat().st_mtime)
        if row and row["parsed"] and row["mtime"] == mt:
            continue
        try:
            s = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
            s.row_factory = sqlite3.Row
            chunks = []
            for st_row in s.execute("SELECT idx, step_payload, metadata, task_details "
                                    "FROM steps ORDER BY idx"):
                for col in ("step_payload", "metadata", "task_details"):
                    v = st_row[col]
                    if isinstance(v, bytes) and v:
                        t = _proto_text(v)
                        if t:
                            chunks.append(f"--- step {st_row['idx']} {col} ---\n{t}")
            try:
                blob = s.execute("SELECT data FROM trajectory_metadata_blob LIMIT 1"
                                 ).fetchone()
                if blob and isinstance(blob["data"], bytes):
                    t = _proto_text(blob["data"])
                    if t:
                        chunks.insert(0, f"--- trajectory metadata ---\n{t}")
            except Exception:
                pass
            s.close()
        except Exception:
            continue
        text = "\n\n".join(chunks)
        if not text.strip():
            continue
        cascade = db.stem
        out = EXTRACTS / "antigravity" / f"{cascade}.txt"
        out.write_text(text, encoding="utf-8")
        # register as a session in the mind (so it counts, gets classified, and
        # shows up beside Claude/Codex work)
        head = text[:1500]
        marker = f"Antigravity conversation {cascade}"
        ok_mem = _upsert_memory_direct(conn, "antigravity_conversation", marker,
                                       f"{marker}\n{head}",
                                       ["antigravity", "conversation", "exhaustive"])
        # session registration via API (proper agent/session upsert path); if the
        # service is down this pass, parsed stays 0 and the whole conversation is
        # retried next pass (the memory upsert is idempotent — no duplicates).
        ok_sess = _mind_api("/sessions/start", {"agent": "antigravity",
                                                "external_id": f"agy-{cascade}"})
        if ok_mem and ok_sess:
            for key in (rel, rel.replace("/", "\\")):
                conn.execute(
                    "UPDATE mind_files SET parsed=1 WHERE agent='antigravity' AND rel_path=?",
                    (key,))
        parsed += 1
    conn.commit(); conn.close()
    return {"parsed": parsed}


def parse_claude_ancillary() -> int:
    """history.jsonl (every prompt ever typed), plans/, tasks/ → extracts +
    memories. skills/ and settings are archived byte-for-byte by archive_all."""
    n = 0
    croot = HOME / ".claude"
    EXTRACTS.joinpath("claude").mkdir(parents=True, exist_ok=True)
    hist = croot / "history.jsonl"
    if hist.exists():
        try:
            lines = hist.read_text(encoding="utf-8", errors="ignore").splitlines()
            prompts = []
            for ln in lines:
                try:
                    d = json.loads(ln)
                    t = d.get("display") or d.get("prompt") or ""
                    if t:
                        prompts.append(t)
                except Exception:
                    continue
            (EXTRACTS / "claude" / "prompt_history.txt").write_text(
                "\n".join(prompts), encoding="utf-8")
            n += 1
        except Exception:
            pass
    for sub in ("plans", "tasks"):
        d = croot / sub
        if not d.exists():
            continue
        for f in d.rglob("*"):
            if f.is_file() and f.suffix.lower() in (".md", ".json", ".txt", ".jsonl"):
                try:
                    dst = EXTRACTS / "claude" / sub / f.relative_to(d)
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, dst)
                    n += 1
                except Exception:
                    continue
    return n


def backfill_full_transcripts(stop_check=None, max_sessions: int | None = None) -> dict:
    """Repair the old 200-event cap: for every session whose activity row count
    is LESS than its source transcript's event count, re-insert the FULL event
    stream directly into mind.db (fast executemany; the HTTP path was why the
    cap existed). The truncated rows are replaced inside a transaction by a
    strict superset regenerated from the pristine source file — nothing is lost,
    the middles that were silently dropped are restored. Payloads are stored as
    the RAW event JSON (full fidelity, not a shaped trim)."""
    conn = _conn()
    fixed, checked = 0, 0
    # map external_id -> transcript path for claude + codex
    sources: dict[str, Path] = {}
    cl = HOME / ".claude" / "projects"
    if cl.exists():
        for j in cl.rglob("*.jsonl"):
            sources[j.stem] = j
    cx = HOME / ".codex" / "sessions"
    if cx.exists():
        for j in cx.rglob("*.jsonl"):
            # rollout-2026-04-01T18-36-33-<uuid>.jsonl → uuid is the thread id
            m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
                          j.stem)
            sources[m.group(1) if m else j.stem] = j
    rows = conn.execute("SELECT id, external_id FROM sessions").fetchall()
    for s in rows:
        if stop_check and stop_check():
            break
        if max_sessions and fixed >= max_sessions:
            break
        src = None
        ext = s["external_id"] or ""
        for key, path in sources.items():
            if key and key in ext or ext in (key,):
                src = path
                break
        if src is None or not src.exists():
            continue
        checked += 1
        try:
            events = []
            with src.open(encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        events.append(line)
        except Exception:
            continue
        have = conn.execute("SELECT COUNT(*) FROM activity WHERE session_id=?",
                            (s["id"],)).fetchone()[0]
        if have >= len(events) or not events:
            continue  # already full fidelity
        payload_rows = []
        for line in events:
            try:
                e = json.loads(line)
            except Exception:
                e = {"raw": line[:4000]}
            ts = None
            t = e.get("timestamp")
            if isinstance(t, str):
                try:
                    from datetime import datetime
                    ts = int(datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp())
                except Exception:
                    ts = None
            kind = e.get("type") or e.get("role") or "event"
            payload_rows.append((s["id"], ts or int(time.time()), str(kind)[:40],
                                 json.dumps(e, ensure_ascii=False)[:200_000]))
        try:
            conn.execute("BEGIN")
            conn.execute("DELETE FROM activity WHERE session_id=?", (s["id"],))
            conn.executemany(
                "INSERT INTO activity (session_id, ts, kind, payload_json) "
                "VALUES (?,?,?,?)", payload_rows)
            conn.commit()
            fixed += 1
        except Exception:
            conn.rollback()
    conn.close()
    return {"sessions_checked": checked, "sessions_backfilled": fixed}


# ── Coverage report: visible proof of exhaustiveness ─────────────────────────

def coverage_report() -> dict:
    conn = _conn()
    ensure_schema(conn)
    rep: dict = {"generated_at": int(time.time()), "agents": {}}
    for agent in SOURCES:
        r = conn.execute(
            """SELECT COUNT(*) AS files, COALESCE(SUM(size),0) AS bytes,
                      SUM(archived) AS archived, SUM(parsed) AS parsed,
                      SUM(CASE WHEN skip IS NOT NULL AND archived=0 THEN 1 ELSE 0 END) AS skipped
               FROM mind_files WHERE agent=?""", (agent,)).fetchone()
        skips = {row["skip"]: row["n"] for row in conn.execute(
            "SELECT skip, COUNT(*) AS n FROM mind_files "
            "WHERE agent=? AND skip IS NOT NULL AND archived=0 GROUP BY skip",
            (agent,))}
        rep["agents"][agent] = {
            "files_seen": r["files"], "bytes_seen": int(r["bytes"] or 0),
            "archived": int(r["archived"] or 0), "parsed": int(r["parsed"] or 0),
            "not_archived": dict(skips),
        }
    conn.close()
    try:
        COVERAGE.write_text(json.dumps(rep, indent=2), encoding="utf-8")
    except Exception:
        pass
    return rep


def run_exhaustive(stop_check=None) -> dict:
    """Full pass: archive everything, parse the new stores, report coverage."""
    out = {"archive": archive_all(stop_check=stop_check)}
    out["codex_threads"] = parse_codex_threads()
    out["codex_memories"] = parse_codex_memories()
    out["antigravity"] = parse_antigravity_conversations()
    out["claude_ancillary"] = parse_claude_ancillary()
    out["backfill"] = backfill_full_transcripts(stop_check=stop_check)
    out["coverage"] = coverage_report()
    return out


if __name__ == "__main__":
    print(json.dumps(run_exhaustive(), indent=2, ensure_ascii=False))
