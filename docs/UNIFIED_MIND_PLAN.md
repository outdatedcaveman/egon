# Egon — Unified Mind for Multi-Agent Work

**Status:** v1 foundation in flight (2026-05-28). This doc describes the
design Bruno asked for: every AI Bruno uses (Claude, Codex, ChatGPT,
Gemini, Antigravity, …) acts as a different *body* of one shared mind
that lives in Egon. Shared memory, shared project state, shared activity
log, shared goals. Picking up another agent's work is zero-friction —
full context appears instantly. Nothing overwrites anything. Every
action is recorded for future improvement.

This complements (does not replace) `docs/RECONCILE_2026-05-27.md`,
which is the operational record of the embedding work.

---

## Why this is needed (evidence in the wild)

When we hunted for the standalone Routster project today, every agent
turned out to have its own private memory of it:

- **Claude** — `~/.claude/projects/…/memory/project_mouseion.md` (and
  Routster-adjacent entries).
- **Codex** — `~/.codex/memories/rollout_summaries/…mouseion_enrichment_probe_hard_tail_recovery.md`.
- **Antigravity (Gemini)** — `~/.gemini/antigravity/brain/<session>/routster_v3_plan.md`,
  plus screenshots: `routster_loaded_*.png`, `verify_routster_launch_*.webp`.

Three different agents, three private memories, none of them can see the
others'. When Bruno asks one of them to continue work the others
started, the new agent has to be re-briefed manually — exactly the
friction he wants gone.

## The shape

```
                      ┌──────────────────────────┐
                      │   Egon (the unified hub) │
                      │  • SQLite mind.db        │
                      │  • Panop FastAPI :8000   │
                      │  • Mind UI tab           │
                      └────────────┬─────────────┘
                                   │ REST + file polling
              ┌────────────────────┼────────────────────┐
              │                    │                    │
       ┌──────┴─────┐       ┌──────┴─────┐       ┌──────┴─────┐
       │ Claude     │       │ Codex      │       │ Antigravity │
       │ Code body  │       │ body       │       │ (Gemini)    │
       └────────────┘       └────────────┘       └─────────────┘
              │                    │                    │
              └─── ChatGPT body ───┴── Gemini body ─────┘
```

Each *body* writes its activity and reads shared context through Egon.
Egon stores everything in a single SQLite (`state/mind.db`) for
portability and clean transactional semantics.

## Core capabilities

| Capability | What it means |
|---|---|
| **Unified memory** | Facts, preferences, decisions, learned patterns. Read/write by any agent. |
| **Project registry** | Every project's purpose, state, owner-agent at last touch, related files. |
| **Activity log** | Every agent action — file edit, command, decision, hypothesis — timestamped + attributed + linked to project. Append-only. |
| **Context broker** | Given (agent, project, optional query), surfaces the most-relevant memory + recent activity. Eliminates the manual "explain what's been done" step. |
| **Coordination / locking** | Soft + strong leases on files so agent B sees that agent A is editing X before touching it. |
| **Audit + improvement** | Every action recorded. Periodic introspection. Mineable for what worked vs. didn't. |
| **File index** | Canonical record per file under management (path, content hash, last-editor session, related project). |

## Architecture

### Storage — `state/mind.db` (SQLite + WAL)

```sql
-- the agents that act on the mind
CREATE TABLE agents (
  id INTEGER PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,        -- 'claude-code', 'codex', 'antigravity', 'chatgpt', 'gemini'
  kind TEXT NOT NULL,               -- 'ide-agent' | 'web-agent' | 'background'
  created_at INTEGER NOT NULL
);

-- ongoing or completed work units
CREATE TABLE projects (
  id INTEGER PRIMARY KEY,
  slug TEXT UNIQUE NOT NULL,        -- 'egon', 'routster', 'synesism', ...
  name TEXT NOT NULL,
  description TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  root_path TEXT,                   -- absolute path on disk if any
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

-- one row per agent <-> work session (transcript, rollout, brain folder, ...)
CREATE TABLE sessions (
  id INTEGER PRIMARY KEY,
  agent_id INTEGER NOT NULL REFERENCES agents(id),
  project_id INTEGER REFERENCES projects(id),
  external_id TEXT,                 -- Claude session UUID, Codex thread_id, Antigravity session UUID, ...
  started_at INTEGER NOT NULL,
  ended_at INTEGER,
  summary TEXT,                     -- short natural-language summary at session end
  UNIQUE (agent_id, external_id)
);

-- atomic actions inside a session
CREATE TABLE activity (
  id INTEGER PRIMARY KEY,
  session_id INTEGER NOT NULL REFERENCES sessions(id),
  ts INTEGER NOT NULL,
  kind TEXT NOT NULL,               -- 'file_edit' | 'command' | 'decision' | 'hypothesis' | 'note' | 'error' | 'finding'
  payload_json TEXT NOT NULL        -- shape depends on kind; JSON for flexibility
);
CREATE INDEX activity_session ON activity (session_id, ts);
CREATE INDEX activity_ts ON activity (ts);

-- durable facts / preferences / decisions / skills the mind holds
CREATE TABLE memory (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,               -- 'fact' | 'preference' | 'decision' | 'skill' | 'pattern'
  content TEXT NOT NULL,            -- markdown
  tags TEXT,                        -- comma-separated for quick filtering
  attribution_agent_id INTEGER REFERENCES agents(id),
  attribution_session_id INTEGER REFERENCES sessions(id),
  related_memory_ids TEXT,          -- comma-separated, the graph edges
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE INDEX memory_kind ON memory (kind);
CREATE INDEX memory_tags ON memory (tags);

-- file-level audit + soft locking
CREATE TABLE files (
  id INTEGER PRIMARY KEY,
  project_id INTEGER REFERENCES projects(id),
  path TEXT UNIQUE NOT NULL,
  content_hash TEXT,
  last_editor_session_id INTEGER REFERENCES sessions(id),
  last_edited_at INTEGER,
  lease_session_id INTEGER REFERENCES sessions(id),
  lease_expires_at INTEGER          -- NULL when not leased
);
CREATE INDEX files_project ON files (project_id);
```

### API surface — `/api/v1/mind/*`

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/mind/agents/register` | Idempotent. `{name, kind}` → `{id}`. |
| POST | `/api/v1/mind/projects` | Upsert by slug. |
| GET | `/api/v1/mind/projects` | List. |
| POST | `/api/v1/mind/sessions/start` | `{agent, external_id, project?}` → `{session_id}`. |
| POST | `/api/v1/mind/sessions/end` | `{session_id, summary}`. |
| POST | `/api/v1/mind/activity` | Append. `{session_id, kind, payload}`. |
| GET | `/api/v1/mind/activity` | Filter by `?project=...&agent=...&since=...&limit=...`. |
| POST | `/api/v1/mind/memory` | Upsert. `{id?, kind, content, tags, attribution_session}` → `{id}`. |
| GET | `/api/v1/mind/memory` | Search by `?kind=...&tags=...&q=...`. |
| GET | `/api/v1/mind/context` | `?project=...&query=...` → `{recent_activity, relevant_memory, active_sessions}`. The context broker. |
| POST | `/api/v1/mind/files/lease` | Soft lease on a path. |
| GET | `/api/v1/mind/files/leases` | Who's holding what. |

## Per-agent integration

### Two strategies, layered

1. **PULL (v1, this week)** — Egon polls each agent's local memory dir
   on a schedule and ingests new artifacts. **No agent-side change
   required**, which means we can have unified state TOMORROW without
   needing Claude Code's, Codex's, or Antigravity's cooperation.
2. **PUSH (v2)** — agents proactively call Egon's REST API at session
   boundaries and on important events. More real-time, requires
   per-agent hook configuration.

### What to ingest, per agent

| Agent | Source | What we ingest |
|---|---|---|
| Claude Code | `~/.claude/projects/<slug>/` JSONL transcripts + `memory/*.md` | One session per JSONL; activity rows per Tool use; memory rows from `memory/*.md`. |
| Codex | `~/.codex/sessions/.../rollout-*.jsonl` + `~/.codex/memories/rollout_summaries/*.md` | Session per rollout; activity per tool use; memory per summary. |
| Antigravity (Gemini IDE) | `~/.gemini/antigravity/brain/<session>/*.md` + screenshots | Session per brain folder; memory per plan/note `.md`; files index for the screenshots (so other agents can see the plan was drawn). |
| hermes-agent | `~/.hermes/state.db` SQLite + `~/.hermes/memories/*.md` | Session + activity turns parsed from SQLite; `MEMORY.md` and `USER.md` rules assets. |
| ChatGPT / Gemini web | n/a for pull (cloud-hosted) | v2 only — Custom GPT actions / Gemini Extensions calling Egon's REST API. |
| Future agents | a generic `~/.ai-bodies/<agent>/inbox/` dir | Anything dropped into the inbox is ingested. Lowest-friction integration. |

### How Claude Code's body talks to the mind (v2, after pull lands)

- **At session start (`UserPromptSubmit` hook):** GET
  `/api/v1/mind/context?project=<inferred>&query=<first user message>` →
  prepend the response to the user message under a `<shared-context>`
  block. The session sees what every other body has done.
- **On each tool call (`PostToolUse` hook):** POST
  `/api/v1/mind/activity` with `{kind:"<tool>", payload:{...}}`. Real-
  time visibility for whoever's looking.
- **At session end (`Stop` hook):** POST `/api/v1/mind/sessions/end`
  with a one-paragraph summary. The mind gets a digest.

Equivalent integration patterns for Codex and Antigravity are documented
once they're built.

## Phased rollout

### Phase A — v1 foundation (in flight, 2026-05-28)

1. **`external/panop_server/mind_endpoints.py`** — SQLite schema +
   CRUD endpoints listed above, registered on the Panop FastAPI as a
   sibling module (per the "add, don't reinvent" pattern).
2. **Smoke test** — agents register, projects upsert, activity append/
   query round-trips, context endpoint returns the right shape.
3. **Document** — this file is the canonical spec.

### Phase B — Pull ingestion (next)

1. **`lib/mind_ingest.py`** — periodic poll of the three agent dirs.
   Idempotent (uses `external_id` uniqueness on `sessions`).
2. **Wire into Egon's main loop** — a QTimer that runs every 60 s while
   Egon is open (per the "nothing runs outside Egon" rule).
3. **Mind UI tab** — a feed view in `egon_app/pages/mind.py` showing
   recent activity across all agents, project list, memory search.

### Phase C — Push integration (Claude Code first)

1. Hook configuration in `~/.claude/settings.local.json` (Stop,
   UserPromptSubmit, PostToolUse) that POST to Egon's API.
2. Verify the round-trip with a test session.

### Phase D — Codex + Antigravity + Hermes integration

1. Codex doesn't have a hook system as of writing — pull stays the integration. If/when that changes, port the Claude pattern.
2. Antigravity does have an extension layer; replicate the Claude pattern via that.
3. Hermes Agent pulls local state via `state.db` SQLite parsing and ingests rule assets (`MEMORY.md` and `USER.md`). Added in June 2026.

### Phase E — Cloud bodies (ChatGPT, Gemini web)

1. Build a Custom GPT with Actions calling Egon's REST API. Bruno
   shares the GPT with his other accounts as needed.
2. Build a Gemini Extension (Function Calling) doing the same.

### Phase F — Coordination & improvement

1. Active-leases UI in the Mind tab so each body can see what others
   are editing right now.
2. Periodic introspection job: which memory rows actually surface in
   contexts, which never do, what activity patterns recur.

## Open decisions

- **Embeddings for context retrieval?** v1 returns recent activity +
  tag-filtered memory. v2 could add a local embedding model
  (sentence-transformers / Ollama) for semantic search. Embeds 90 MB+
  into the bundle — defer until v1 proves out.
- **Privacy boundary** — by Bruno's standing rule, **nothing leaves
  127.0.0.1**. All mind data is local SQLite. Cloud bodies (Phase E)
  must talk to Egon, not the other way around (Egon never POSTs out).
- **Versioning** — `state/mind.db` schema is versioned in a
  `mind_schema_version` PRAGMA. Each migration adds a numbered .sql
  file under `lib/mind/migrations/`.
- **Backup** — included in the existing `state/` backup. No new path.

## v1 success criterion

By the end of Phase A:

- Panop responds 200 on all `/api/v1/mind/*` endpoints listed above.
- A test script can register an agent, create a project, start a
  session, append three activity rows, end the session, and read back
  the context — all round-trip correctly.
- `state/mind.db` is created on first POST and survives restarts.
- Egon's main.py imports cleanly. No regression on the existing 14-PASS
  Phase-1 test.
