"""Mind ingestion — pull-based v1.

Polls Claude Code, Codex, and Antigravity (Gemini) memory + transcript
dirs every INGEST_INTERVAL_S seconds while Egon is open. New artifacts
become rows in state/mind.db via the local /api/v1/mind/* endpoints.

Per docs/UNIFIED_MIND_PLAN.md (2026-05-28): this is the lowest-friction
path to a shared mind across agents — no per-agent cooperation
required; we just read what each agent already writes to disk.

Sources (existence checked at each poll, missing dirs skipped):
  • Claude Code
      ~/.claude/projects/<slug>/<session-uuid>.jsonl   — transcripts
      ~/.claude/projects/<slug>/memory/*.md            — memory files
  • Codex
      ~/.codex/sessions/<yyyy>/<mm>/<dd>/rollout-*.jsonl — sessions
      ~/.codex/memories/rollout_summaries/*.md          — summaries
  • Antigravity (Gemini)
      ~/.gemini/antigravity/brain/<session-uuid>/*.md   — plans/notes

State (which files we've already processed) lives at
state/mind_ingest_state.json so we don't re-ingest on every poll.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

import requests

USER_HOME = Path.home()
EGON_ROOT = Path(__file__).resolve().parent.parent
MIND_API = "http://127.0.0.1:8000/api/v1/mind"
STATE_PATH = EGON_ROOT / "state" / "mind_ingest_state.json"
INGEST_INTERVAL_S = 60

# Per-pass bounds. Claude transcripts can have thousands of events;
# inserting them all synchronously via HTTP swamps Panop. We cap each
# session's activity to a representative slice (first + last N) and
# the number of new sessions processed per pass. Tunable via the
# config block `egon-config.json.mind_ingest`.
MAX_EVENTS_PER_SESSION = 200
MAX_NEW_SESSIONS_PER_PASS = 30

# Per-agent identity (registered once, reused for every session attribution)
_AGENT_CLAUDE = ("claude-code", "ide-agent")
_AGENT_CODEX = ("codex", "ide-agent")
_AGENT_ANTIGRAVITY = ("antigravity", "ide-agent")


# ── state persistence ──────────────────────────────────────────────────────

def _load_state() -> dict:
    try:
        with STATE_PATH.open(encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    tmp.replace(STATE_PATH)


# ── mind API helpers ───────────────────────────────────────────────────────

def _get(path: str, timeout: float = 1.5) -> dict | None:
    try:
        r = requests.get(f"{MIND_API}{path}", timeout=timeout)
        if r.status_code == 200:
            body = r.json()
            if isinstance(body, dict) and body.get("status") != "error":
                return body
    except Exception:
        return None
    return None


def _post(path: str, body: dict, timeout: float = 4.0) -> dict | None:
    try:
        r = requests.post(f"{MIND_API}{path}", json=body, timeout=timeout)
        if r.status_code == 200:
            body = r.json()
            if isinstance(body, dict) and body.get("status") != "error":
                return body
    except Exception:
        return None
    return None


def _mind_api_ready() -> bool:
    return _get("/stats") is not None


def _register_agent(name: str, kind: str) -> int | None:
    r = _post("/agents/register", {"name": name, "kind": kind})
    return (r or {}).get("id")


def _start_session(agent: str, external_id: str, project: str | None,
                   started_at: int | None = None) -> int | None:
    body = {"agent": agent, "external_id": external_id}
    if project:
        body["project"] = project
    if started_at:
        body["started_at"] = started_at
    r = _post("/sessions/start", body)
    return (r or {}).get("id")


def _end_session(sid: int, summary: str | None = None,
                 ended_at: int | None = None) -> bool:
    body = {"session_id": sid}
    if summary:
        body["summary"] = summary
    if ended_at:
        body["ended_at"] = ended_at
    return _post("/sessions/end", body) is not None


def _codex_payload_text(inner: dict) -> str:
    """Pull human-readable text from a Codex rollout payload. Content can be
    a plain string, a list of {type, text} segments, or live under
    text/message; compacted events carry a replacement_history."""
    c = inner.get("content")
    if isinstance(c, str) and c.strip():
        return c
    if isinstance(c, list):
        t = " ".join(seg.get("text", "") for seg in c
                     if isinstance(seg, dict)).strip()
        if t:
            return t
    for k in ("text", "message"):
        v = inner.get(k)
        if isinstance(v, str) and v.strip():
            return v
    hist = inner.get("replacement_history")
    if isinstance(hist, list):
        for item in hist:
            t = _codex_payload_text(item) if isinstance(item, dict) else ""
            if t:
                return t
    return ""


def _append_activity(sid: int, kind: str, payload: dict,
                     ts: int | None = None) -> bool:
    body = {"session_id": sid, "kind": kind, "payload": payload}
    if ts:
        body["ts"] = ts
    return _post("/activity", body) is not None


def _upsert_memory(kind: str, content: str, tags: list[str] | None = None,
                   attribution_session_id: int | None = None) -> int | None:
    body = {"kind": kind, "content": content,
            "tags": tags or [],
            "attribution_session_id": attribution_session_id}
    r = _post("/memory", body)
    return (r or {}).get("id")


# ── Claude Code ─────────────────────────────────────────────────────────────

_CLAUDE_PROJECTS = USER_HOME / ".claude" / "projects"


def _scan_claude(state: dict) -> int:
    """Returns count of newly-ingested artifacts."""
    seen = state.setdefault("claude", {"transcripts": {}, "memory": {}})
    n = 0
    new_sessions_this_pass = 0
    if not _CLAUDE_PROJECTS.exists():
        return 0
    for project_dir in _CLAUDE_PROJECTS.iterdir():
        if not project_dir.is_dir():
            continue
        slug = _project_slug_from_claude_dir(project_dir.name)

        # Transcripts: <session-uuid>.jsonl at project root
        for jsonl in project_dir.glob("*.jsonl"):
            session_uuid = jsonl.stem
            mtime = int(jsonl.stat().st_mtime)
            already = seen["transcripts"].get(session_uuid, 0)
            if mtime <= already:
                continue
            if new_sessions_this_pass >= MAX_NEW_SESSIONS_PER_PASS:
                break
            n_msgs = _ingest_claude_transcript(slug, session_uuid, jsonl)
            if n_msgs is None:
                continue
            seen["transcripts"][session_uuid] = mtime
            n += n_msgs
            new_sessions_this_pass += 1

        # Memory files: <project>/memory/*.md
        mem_dir = project_dir / "memory"
        if mem_dir.exists():
            for md in mem_dir.glob("*.md"):
                key = f"{slug}::{md.name}"
                mtime = int(md.stat().st_mtime)
                already = seen["memory"].get(key, 0)
                if mtime <= already:
                    continue
                content = _safe_read(md)
                kind = md.stem  # e.g. project_mouseion, user_aspirations
                mid = _upsert_memory(kind=kind, content=content,
                                     tags=["claude", "memory", slug])
                if mid is None:
                    continue
                seen["memory"][key] = mtime
                n += 1
    return n


def _project_slug_from_claude_dir(name: str) -> str | None:
    """Claude encodes project paths in dir names like
    'C--Users-you-Claude-Code--egon'. Defer to the canonical resolver
    so the slug agrees with every other agent's slug for the same project.

    Bruno 2026-05-29: return None (unattributed) when the resolver can't
    derive a real project — the old `or name.lower()` fallback resurrected
    raw encoded dir names like 'c--users-you--claude-mem-observer-sessions'
    as fake projects. Better to leave such sessions unattributed."""
    from lib.mind_project_resolver import canonical_slug
    return canonical_slug(name)


def _ingest_claude_transcript(slug: str, uuid: str, path: Path) -> int | None:
    sid = _start_session(_AGENT_CLAUDE[0], external_id=uuid, project=slug)
    if sid is None:
        return None
    last_ts = None
    activity_count = 0
    # Materialize the JSONL once so we can take a representative slice
    # without making thousands of HTTP calls. Cap from BOTH ends.
    try:
        events = []
        with path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    events.append(json.loads(line))
                except Exception:
                    continue
        if len(events) > MAX_EVENTS_PER_SESSION:
            head = events[:MAX_EVENTS_PER_SESSION // 2]
            tail = events[-(MAX_EVENTS_PER_SESSION // 2):]
            events = head + tail
        for e in events:
            ts_iso = e.get("timestamp")
            ts = _iso_to_epoch(ts_iso) if ts_iso else None
            last_ts = ts or last_ts
            kind = e.get("type") or e.get("role") or "message"
            if not _append_activity(sid, kind=kind,
                                    payload=_shape_claude_event(e), ts=ts):
                return None
            activity_count += 1

            if kind == "assistant" or e.get("type") == "assistant":
                msg = e.get("message") or {}
                usage = msg.get("usage") or {}
                model = msg.get("model")
                if usage and model:
                    content = msg.get("content")
                    tools = []
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "tool_use":
                                n = block.get("name")
                                if n:
                                    tools.append(n)
                    if _post("/ledger/turns", {
                        "session_id": sid,
                        "ts": ts or int(time.time()),
                        "model": model,
                        "usage": usage,
                        "tools": tools
                    }) is None:
                        return None
    except Exception:
        return None
    # Best-effort close — we close every poll; if the session is still
    # being written we re-open via the (agent, external_id) uniqueness
    # constraint, but ended_at gets updated to the latest seen.
    if not _end_session(sid, summary=None, ended_at=last_ts):
        return None
    return activity_count


def _shape_claude_event(e: dict) -> dict:
    out: dict = {}
    for k in ("type", "role", "uuid", "model"):
        if e.get(k):
            out[k] = e[k]
    msg = e.get("message") or {}
    if isinstance(msg, dict):
        content = msg.get("content")
        if isinstance(content, str):
            out["text_preview"] = content[:500]
        elif isinstance(content, list):
            parts = []
            for c in content[:6]:
                if isinstance(c, dict):
                    if c.get("type") == "text":
                        parts.append(("text", (c.get("text") or "")[:300]))
                    elif c.get("type") == "tool_use":
                        parts.append(("tool_use", c.get("name", "")))
                    elif c.get("type") == "tool_result":
                        parts.append(("tool_result", str(c.get("content"))[:200]))
            out["parts"] = parts
    return out


# ── Codex ───────────────────────────────────────────────────────────────────

_CODEX_SESSIONS = USER_HOME / ".codex" / "sessions"
_CODEX_SUMMARIES = USER_HOME / ".codex" / "memories" / "rollout_summaries"


def _scan_codex(state: dict) -> int:
    seen = state.setdefault("codex", {"rollouts": {}, "summaries": {}})
    n = 0

    if _CODEX_SESSIONS.exists():
        new_this_pass = 0
        for jsonl in _CODEX_SESSIONS.rglob("rollout-*.jsonl"):
            uuid = _codex_uuid_from_filename(jsonl.name) or jsonl.stem
            mtime = int(jsonl.stat().st_mtime)
            if mtime <= seen["rollouts"].get(uuid, 0):
                continue
            if new_this_pass >= MAX_NEW_SESSIONS_PER_PASS:
                break
            n_events = _ingest_codex_rollout(uuid, jsonl)
            if n_events is None:
                continue
            n += n_events
            seen["rollouts"][uuid] = mtime
            new_this_pass += 1

    if _CODEX_SUMMARIES.exists():
        for md in _CODEX_SUMMARIES.glob("*.md"):
            mtime = int(md.stat().st_mtime)
            if mtime <= seen["summaries"].get(md.name, 0):
                continue
            content = _safe_read(md)
            slug = _codex_summary_to_project(md.name)
            mid = _upsert_memory(kind="rollout_summary", content=content,
                                 tags=["codex", "summary"] + ([slug] if slug else []))
            if mid is None:
                continue
            seen["summaries"][md.name] = mtime
            n += 1
    return n


def _codex_uuid_from_filename(name: str) -> str | None:
    m = re.search(r"rollout-[\d\-T:]+-(.+?)\.jsonl$", name)
    return m.group(1) if m else None


def _codex_summary_to_project(name: str) -> str | None:
    # e.g. 2026-04-29T01-29-49-7jkJ-mouseion_enrichment_probe_hard_tail_recovery.md
    # Strip the timestamp + 4-char rollout id prefix, then resolve.
    m = re.search(r"^[\d\-T:]+-[A-Za-z0-9]{4}-(.+?)\.md$", name)
    if not m:
        return None
    # The captured fragment is an arbitrary session TITLE, not necessarily a
    # project ("from-recent-prs-and-reviews-suggest" etc.). Only accept it if
    # it resolves to a KNOWN project; otherwise leave unattributed.
    # Bruno 2026-05-29.
    from lib.mind_project_resolver import canonical_slug, known_project_slugs
    slug = canonical_slug(m.group(1))
    return slug if slug in known_project_slugs() else None


# Generic launch directories Bruno opens Codex from that are NOT the subject
# of the work. `flood` == ~/Documents/New project, Codex's default terminal
# dir; he routinely runs Codex from there while editing egon/panop/etc. So a
# cwd of "New project" must NEVER override what the session actually touched.
_CODEX_SCRATCH_SLUGS = {None, "flood", "double"}
# Match a Bruno project root inside a path: "...\Claude Code\egon\..." etc.
_PROJECT_PATH_RE = re.compile(r"[Cc]laude[ _]?[Cc]ode[\\/]+([A-Za-z0-9_.\-]+)")


def _attribute_codex_project(events: list[dict]) -> str | None:
    """Attribute a Codex session to a project by what it EDITED, not the
    directory it was launched from. Codex has no project concept and Bruno
    launches it from a generic 'New project' folder (→ 'flood'), so the cwd
    is a lie — a session can spend its whole life editing egon files while
    cwd says flood. We tally references to `Claude Code\\<project>` paths
    across the rollout and prefer the dominant one over a scratch cwd.
    Bruno 2026-06-13: 'I want a system where all the AIs know everything the
    others do' — attribution by subject is what makes that hold."""
    from lib.mind_project_resolver import canonical_slug
    cwd_project = None
    tally: dict[str, int] = {}
    for i, e in enumerate(events):
        if cwd_project is None and i < 30:
            cwd = e.get("cwd") or e.get("working_directory") or e.get("workdir")
            if not cwd and isinstance(e.get("payload"), dict):
                cwd = e["payload"].get("cwd")
            if cwd and any(c in str(cwd) for c in "/\\:"):
                cwd_project = canonical_slug(cwd)
        try:
            blob = e if isinstance(e, str) else json.dumps(e, ensure_ascii=False)
        except Exception:
            continue
        for seg in _PROJECT_PATH_RE.findall(blob):
            slug = canonical_slug("x/" + seg)   # force last-segment + noise filter
            if slug:
                tally[slug] = tally.get(slug, 0) + 1
    dominant = None
    if tally:
        dominant, dcount = max(tally.items(), key=lambda kv: kv[1])
        total = sum(tally.values())
        if not (dcount >= 3 and dcount >= 0.6 * total):
            dominant = None
    # Prefer edited-content project when the launch dir is a scratch folder
    # (or already agrees). Keep a real cwd project otherwise — cross-project
    # mentions shouldn't yank a legitimately-attributed session away.
    if dominant and (cwd_project in _CODEX_SCRATCH_SLUGS or dominant == cwd_project):
        return dominant
    return cwd_project or dominant


def _ingest_codex_rollout(uuid: str, path: Path) -> int | None:
    try:
        events = []
        with path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    events.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return None

    project = _attribute_codex_project(events)

    sid = _start_session(_AGENT_CODEX[0], external_id=uuid, project=project)
    if sid is None:
        return None
    last_ts = None
    n = 0
    try:
        if len(events) > MAX_EVENTS_PER_SESSION:
            head = events[:MAX_EVENTS_PER_SESSION // 2]
            tail = events[-(MAX_EVENTS_PER_SESSION // 2):]
            events = head + tail
        for e in events:
            ts_iso = e.get("timestamp") or e.get("created_at")
            ts = _iso_to_epoch(ts_iso) if ts_iso else None
            last_ts = ts or last_ts
            kind = e.get("type") or e.get("role") or "event"
            payload = {"raw_keys": list(e.keys())[:10]}
            if isinstance(e.get("content"), str):
                payload["content_preview"] = e["content"][:400]
            elif isinstance(e.get("payload"), dict):
                # Codex rollouts nest substance under "payload", and content
                # is usually a LIST of segments [{type, text}] — the original
                # string-only check missed 100% of it (the husk flood).
                inner = e["payload"]
                txt = _codex_payload_text(inner)
                if txt:
                    payload["content_preview"] = txt[:400]
                    if inner.get("role"):
                        payload["role"] = inner["role"]
            if "content_preview" not in payload:
                # Contentless husk. One Codex session once flooded the mind
                # with 105k of these ({"raw_keys": [...]} only) — over half
                # of all activity rows, zero information. Skip; the session
                # row itself still records that the session happened.
                # 2026-06-12 cleanup; husks archived to mind_archive.db.
                continue
            if not _append_activity(sid, kind=kind, payload=payload, ts=ts):
                return None
            n += 1
    except Exception:
        pass
    if not _end_session(sid, ended_at=last_ts):
        return None
    return n


# ── Antigravity (Gemini) ────────────────────────────────────────────────────

_AG_BRAIN = USER_HOME / ".gemini" / "antigravity" / "brain"


# -- agent assets: skills, rules, configs --------------------------------------
# Bruno 2026-06-12: "is absolutely every file, skill, memory, context, rules,
# customization from all 3 AIs being shared in an ACTUAL FUNCTIONAL AND
# ACCESSIBLE WAY?" Transcripts/brain/summaries were; skills + global rules +
# configs were NOT. This scanner ingests each as ONE durable memory row
# (kind='agent_asset', stable id tracked in state["assets"] so updates edit
# the same row), making them searchable by every agent through the capsule
# and mind_memory tools.

def _frontmatter_desc(text: str) -> str:
    """name/description out of a SKILL.md frontmatter, best effort."""
    out = []
    for line in text.splitlines()[:30]:
        ls = line.strip()
        if ls.startswith(("name:", "description:")):
            out.append(ls)
    return " | ".join(out)


# Skill roots per agent. Skills don't live in one folder — they're scattered
# across plugin caches, vendor imports and IDE plugin dirs. 2026-06-12: the
# old scanner only checked the TOP level of two dirs and found 2 Codex skills;
# the real counts are Claude ~136+110(plugins), Codex ~196 (plugins/cache +
# vendor_imports), Antigravity ~49. We rglob each root for SKILL.md and dedup
# by skill name (parent-dir) so each capability is ingested once.
_SKILL_ROOTS = {
    "claude-code": [".claude/skills", ".claude/plugins"],
    "codex": [".codex/skills", ".codex/plugins/cache", ".codex/vendor_imports/skills"],
    "antigravity": [".gemini/config/plugins", ".gemini/antigravity-ide/plugins"],
}


def _iter_assets():
    """Yield (key, agent, asset_kind, path, max_chars)."""
    H = USER_HOME
    fixed = [
        ("claude:settings", "claude-code", "rules",
         H / ".claude" / "settings.local.json", 4000),
        ("codex:agents-md", "codex", "rules", H / ".codex" / "AGENTS.md", 12000),
        ("codex:config", "codex", "config", H / ".codex" / "config.toml", 6000),
        ("antigravity:gemini-md", "antigravity", "rules",
         H / ".gemini" / "GEMINI.md", 12000),
    ]
    for key, agent, kind, path, cap in fixed:
        if path.exists():
            yield key, agent, kind, path, cap

    for agent, roots in _SKILL_ROOTS.items():
        seen_names: set[str] = set()
        for rel in roots:
            root = H / rel
            if not root.is_dir():
                continue
            for md in root.rglob("SKILL.md"):
                # skip transient/legacy/backup copies — only canonical skills
                low = str(md).lower()
                if any(x in low for x in (".tmp", "legacy", "backup")):
                    continue
                name = md.parent.name
                if name in seen_names:
                    continue
                seen_names.add(name)
                yield f"{agent}:skill:{name}", agent, "skill", md, 1600


def _scan_agent_assets(state: dict) -> int:
    seen = state.setdefault("assets", {})
    n = 0
    for key, agent, kind, path, cap in _iter_assets():
        try:
            mtime = int(path.stat().st_mtime)
        except OSError:
            continue
        rec = seen.get(key) or {}
        if mtime <= rec.get("mtime", 0) and rec.get("mid"):
            continue
        text = _safe_read(path)[:cap]
        if not text.strip():
            continue
        head = _frontmatter_desc(text) if kind == "skill" else ""
        content = (f"[{agent} {kind}] {path.name}"
                   + (f" — {head}" if head else "")
                   + f"\npath: {path}\n\n{text}")
        body = {"kind": "agent_asset", "content": content[:cap + 400],
                "tags": f"asset,{kind},{agent},shared"}
        if rec.get("mid"):
            body["id"] = rec["mid"]
        r = _post("/memory", body)
        if r is None:
            continue
        seen[key] = {"mtime": mtime, "mid": (r or {}).get("id") or rec.get("mid")}
        n += 1
    return n


def _scan_antigravity(state: dict) -> int:
    seen = state.setdefault("antigravity", {"notes": {}})
    n = 0
    if not _AG_BRAIN.exists():
        return 0
    for session_dir in _AG_BRAIN.iterdir():
        if not session_dir.is_dir():
            continue
        session_uuid = session_dir.name
        # Resolve the project ONCE per session from its CONTENT, not from the
        # generic filenames (implementation_plan.md / task.md / walkthrough.md
        # would otherwise yield garbage slugs like "implementation"/"task").
        # Bruno 2026-05-29.
        project = _antigravity_project_for_session(session_dir)
        for md in session_dir.glob("*.md"):
            key = f"{session_uuid}::{md.name}"
            mtime = int(md.stat().st_mtime)
            if mtime <= seen["notes"].get(key, 0):
                continue
            content = _safe_read(md)
            kind = "plan" if "plan" in md.name.lower() else "note"
            # Ensure a session row exists so future analytics can correlate
            # brain notes with timelines, and so the memory row has attribution.
            sid = _start_session(_AGENT_ANTIGRAVITY[0],
                                 external_id=session_uuid,
                                 project=project)
            if sid is None:
                continue
            mid = _upsert_memory(kind=kind, content=content,
                                 tags=["antigravity", project] if project else ["antigravity"],
                                 attribution_session_id=sid)
            if mid is None:
                continue
            seen["notes"][key] = mtime
            n += 1
    return n


# Regexes for pulling the real project out of an Antigravity brain session.
_AG_SCRATCH_RE = re.compile(r"scratch[/\\]+([A-Za-z0-9][A-Za-z0-9_.-]*)",
                            re.IGNORECASE)
_AG_TITLE_RE = re.compile(r"^#\s*([^\n#][^\n]*)", re.MULTILINE)


def _antigravity_project_for_session(session_dir: Path) -> str | None:
    """Derive the canonical project slug for an Antigravity brain session by
    reading its plan/task/walkthrough content — the real project identity
    lives there (a `scratch/<app>` path, an opened workspace folder, or the
    plan's title), never in the generic filenames.

    Resolution order (first hit wins):
      1. A `scratch/<name>` path  → e.g. `scratch/double-app` → "double".
      2. A workspace path outside the brain dir (the folder the user opened).
      3. The first Markdown title heading → matched against known slugs.
    Returns None (unattributed) rather than a garbage slug when unsure.
    """
    from lib.mind_project_resolver import canonical_slug

    texts: list[str] = []
    for fname in ("implementation_plan.md", "task.md", "walkthrough.md",
                  "walkthrough.md.metadata.json", "task.md.metadata.json"):
        fp = session_dir / fname
        if fp.exists():
            texts.append(_safe_read(fp, cap=20_000))
    blob = "\n".join(texts)
    if not blob:
        return None

    # 1) scratch/<app> — strongest signal for Antigravity's own scratch apps.
    m = _AG_SCRATCH_RE.search(blob)
    if m:
        slug = canonical_slug(m.group(1))
        if slug:
            return slug

    # 2) An opened workspace path that is NOT inside the brain/.gemini tree.
    for pm in re.finditer(r"[A-Za-z]:\\\\?(?:[^\s\"'<>|]+\\\\?)+", blob):
        path = pm.group(0).replace("\\\\", "\\")
        low = path.lower()
        if ".gemini" in low or "antigravity" in low or "appdata" in low:
            continue
        slug = canonical_slug(path)
        if slug:
            return slug

    # 3) Title heading → known-slug match ONLY. canonical_slug echoes unknown
    #    candidates back, so we must reject anything that isn't a slug we're
    #    confident about — otherwise "Implementation Plan…" becomes a bogus
    #    "implementation" project. Bruno 2026-05-29.
    from lib.mind_project_resolver import known_project_slugs
    known = known_project_slugs()
    tm = _AG_TITLE_RE.search(blob)
    if tm:
        title = tm.group(1)
        for token in [title] + re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}", title):
            cand = canonical_slug(token)
            if cand and cand in known:
                return cand
    return None


# ── helpers ─────────────────────────────────────────────────────────────────

def _safe_read(path: Path, cap: int = 50_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:cap]
    except Exception:
        return ""


def _iso_to_epoch(s: str) -> int | None:
    if not s:
        return None
    try:
        # Tolerate trailing Z and microsecond variants
        from datetime import datetime
        for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
                    "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
                    "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
            try:
                return int(datetime.strptime(s, fmt).timestamp())
            except Exception:
                continue
    except Exception:
        pass
    return None


# ── one-shot + service ──────────────────────────────────────────────────────

def ingest_once() -> dict:
    """Run one ingestion pass. Returns counts per source."""
    if not _mind_api_ready():
        return {
            "status": "error",
            "error": "mind_api_offline",
            "hint": "Panop must be listening on http://127.0.0.1:8000/api/v1/mind",
        }
    state = _load_state()
    for name, kind in (_AGENT_CLAUDE, _AGENT_CODEX, _AGENT_ANTIGRAVITY):
        if _register_agent(name, kind) is None:
            return {
                "status": "error",
                "error": f"agent_register_failed:{name}",
                "hint": "ingest state was not advanced; this pass will retry later",
            }
    counts = {
        "status": "ok",
        "claude": _scan_claude(state),
        "codex": _scan_codex(state),
        "antigravity": _scan_antigravity(state),
        "agent_assets": _scan_agent_assets(state),
    }
    state["last_ingest_at"] = int(time.time())
    _save_state(state)
    return counts


class MindIngestService:
    """Daemon-thread service started by egon_app/main.py at boot,
    stopped on QApplication.aboutToQuit. Same shape as
    PhoneKeepaliveService — start/stop, idempotent, dies with Egon."""

    def __init__(self, interval_s: int = INGEST_INTERVAL_S):
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._interval = interval_s
        self.last_result: dict | None = None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_loop,
                                        daemon=True,
                                        name="egon-mind-ingest")
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self._thread is not None:
            try:
                self._thread.join(timeout=timeout)
            except Exception:
                pass

    def _run_loop(self) -> None:
        # First pass after a short delay so Panop has time to come up.
        self._stop.wait(8.0)
        while not self._stop.is_set():
            try:
                self.last_result = ingest_once()
            except Exception as e:
                self.last_result = {
                    "status": "error",
                    "error": f"{type(e).__name__}: {str(e)[:200]}",
                }
            self._stop.wait(self._interval)


if __name__ == "__main__":
    # Allow `python -m lib.mind_ingest` for an ad-hoc one-shot pass.
    print(json.dumps(ingest_once(), indent=2))
