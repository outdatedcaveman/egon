# Cross-agent sharing audit — 2026-06-12

Bruno's question: "is absolutely every file, skill, memory, context, rules,
customization from all 3 AIs being shared with all others in an ACTUAL
FUNCTIONAL AND ACCESSIBLE WAY?"

Honest answer at audit time: **no** — and one ingestion bug meant even the
covered channel was silently empty for Codex. Both fixed below.

## What was already shared (and verified working)
| Asset | Path | Channel |
|---|---|---|
| Claude transcripts | ~/.claude/projects/**.jsonl | pull ingest → activity |
| Claude project memory | ~/.claude/projects/*/memory/*.md | pull ingest → memory |
| Antigravity brain notes | ~/.gemini/antigravity/brain/**.md | pull ingest → memory |
| Codex rollout summaries | ~/.codex/memories/rollout_summaries | pull ingest → memory |
| Durable decisions | hooks + MCP mind_memory_upsert | push → memory |
| Live context | mind_context_v2 capsule (MCP in all 3) | pull on demand |

## What was broken: Codex session CONTENT (the "flood" incident)
Codex rollouts nest text under payload.content as a LIST of segments;
the ingester only read top-level string content → it stored 185,859
contentless {"raw_keys"} husks (54% of all activity, project `flood`)
and captured ZERO actual Codex conversation text, ever.
Fixed: _codex_payload_text handles segment lists / text / message /
replacement_history. Husks archived to mind_archive.db; flood's 25
sessions re-ingested clean: 1,598 rows, 100% with content.

## What was NOT shared at all (now is)
| Asset | Count | Now ingested as |
|---|---|---|
| Claude skills (~/.claude/skills/*/SKILL.md) | ~136 | memory kind=agent_asset |
| Codex skills (~/.codex/skills/*) | 2 | memory kind=agent_asset |
| Codex global rules (~/.codex/AGENTS.md) | 1 | memory kind=agent_asset |
| Codex config (~/.codex/config.toml) | 1 | memory kind=agent_asset |
| Antigravity global rules (~/.gemini/GEMINI.md) | 1 | memory kind=agent_asset |
| Claude hooks/rules (settings.local.json) | 1 | memory kind=agent_asset |

Mechanism: _scan_agent_assets in lib/mind_ingest.py — one stable memory row
per asset (state["assets"] maps asset→memory id, mtime-gated, edits update
the same row). Runs in the 60s ingest loop. 140 assets ingested first pass.

## Accessibility proof
GET /api/v1/mind/context/v2?query=codex+playwright+skill returned the Codex
playwright skill + Claude skills in durable_memory — i.e. any agent asking
the capsule (the standard MCP path) can now discover the others' skills,
rules and configs.

## Still deliberately NOT shared
- Claude plugins binaries / caches (machine-specific, no knowledge value)
- Antigravity internals (code_tracker, browser profiles)
- auth.json / tokens (never enter the mind)
