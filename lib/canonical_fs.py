"""Canonical filesystem exporter — materialize the browsable canonical tree.

The mind DB is the source of truth (sessions/memory/docs from every AI, plus
Egon's canonical_assignments). This exporter renders that into a human- and
agent-browsable tree under the shared AI workspace, so ALL AIs (and Bruno) can
walk the canonical project structure Egon built by classification:

    ~/AI/projects/<canonical_project>/
        README.md          — profile + live counts + index
        sessions/<agent>-<external_id>.md   — per-session capsule + source pointer
        _index.json        — machine-readable manifest

Non-destructive: only Egon's own outputs are (re)written; nothing else in the
tree is touched or deleted. Idempotent — safe to re-run every cycle.
Bruno 2026-07-01.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from lib import egon_paths
from lib.mind_context_broker import DB_PATH
from lib.canonical_classifier import CANONICAL_DEFS

CANON_ROOT = egon_paths.SHARED_PROJECTS   # ~/AI/projects
_AGENTS = {1: "claude", 2: "codex", 3: "antigravity"}


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(DB_PATH), timeout=15)
    c.row_factory = sqlite3.Row
    return c


def _safe(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in name)[:80]


def _agent_name(conn: sqlite3.Connection, agent_id) -> str:
    if agent_id in _AGENTS:
        return _AGENTS[agent_id]
    try:
        r = conn.execute("SELECT name FROM agents WHERE id=?", (agent_id,)).fetchone()
        return (r["name"] if r else "ai").split(":")[0]
    except Exception:
        return "ai"


def export_canonical(root: Path | None = None) -> dict:
    """Render every canonical project that has assignments. Returns a summary."""
    base = Path(root) if root else CANON_ROOT
    base.mkdir(parents=True, exist_ok=True)
    conn = _conn()
    # group session assignments by canonical project
    rows = conn.execute(
        """SELECT ca.item_id, ca.canonical_project, ca.confidence, ca.method, ca.rationale,
                  s.external_id, s.agent_id, s.started_at, s.summary
           FROM canonical_assignments ca
           JOIN sessions s ON s.id = CAST(ca.item_id AS INTEGER)
           WHERE ca.item_type='session'""").fetchall()
    by_proj: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        by_proj.setdefault(r["canonical_project"] or "unfiled", []).append(r)

    written = 0
    projects_out = {}
    for proj, sess in by_proj.items():
        slug = _safe(proj.replace("new:", ""))
        pdir = base / slug
        (pdir / "sessions").mkdir(parents=True, exist_ok=True)
        # per-session capsules
        for r in sess:
            agent = _agent_name(conn, r["agent_id"])
            fname = f"{agent}-{_safe(r['external_id'] or r['item_id'])}.md"
            started = time.strftime("%Y-%m-%d %H:%M",
                                    time.localtime(r["started_at"] or 0))
            body = (
                f"# Session {r['external_id']}\n\n"
                f"- agent: **{agent}**\n- started: {started}\n"
                f"- canonical project: **{proj}** "
                f"(via {r['method']}, conf {r['confidence']}; {r['rationale']})\n"
                f"- source transcript: {agent} store, id `{r['external_id']}`\n\n"
                f"## Summary\n\n{r['summary'] or '(no summary)'}\n"
            )
            (pdir / "sessions" / fname).write_text(body, encoding="utf-8")
            written += 1
        # project README + manifest
        agents_seen = sorted({_agent_name(conn, r["agent_id"]) for r in sess})
        readme = (
            f"# {proj}\n\n{CANONICAL_DEFS.get(proj, '(project discovered by classification)')}\n\n"
            f"- sessions: **{len(sess)}**  ·  agents: {', '.join(agents_seen)}\n"
            f"- canonical structure built by Egon's content classifier\n"
            f"- source of truth: mind.db (canonical_assignments)\n\n## Sessions\n\n"
            + "\n".join(
                f"- [{_agent_name(conn, r['agent_id'])}] "
                f"{(r['summary'] or '').splitlines()[0][:90] if r['summary'] else r['external_id']}"
                for r in sorted(sess, key=lambda x: x["started_at"] or 0, reverse=True))
            + "\n")
        (pdir / "README.md").write_text(readme, encoding="utf-8")
        (pdir / "_index.json").write_text(json.dumps({
            "project": proj, "sessions": len(sess), "agents": agents_seen,
            "exported_at": int(time.time()),
            "session_ids": [r["item_id"] for r in sess],
        }, indent=2), encoding="utf-8")
        projects_out[proj] = len(sess)

    # top-level index
    (base / "_CANONICAL_INDEX.md").write_text(
        "# Canonical projects (built by Egon from all AIs' work)\n\n"
        + "\n".join(f"- **{p}** — {n} sessions"
                    for p, n in sorted(projects_out.items(), key=lambda x: -x[1]))
        + "\n", encoding="utf-8")
    conn.close()
    return {"projects": len(projects_out), "sessions_written": written,
            "by_project": dict(sorted(projects_out.items(), key=lambda x: -x[1])),
            "root": str(base)}


if __name__ == "__main__":
    print(json.dumps(export_canonical(), indent=2, ensure_ascii=False))
