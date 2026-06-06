# Egon — project-local Claude Code context

Sister projects (Routster, Mouseion, Panop, Synesism, InfoHub, CareerOps) all share Egon's unified mind: a SQLite store at `state/mind.db` exposed on `http://127.0.0.1:8000/api/v1/mind/*`. The API can run through Egon's desktop app or the standalone launcher `Start Egon Mind Service.bat`. Three independent layers populate it:

1. **Pull** — `lib/mind_ingest.py` polls `~/.claude/projects/`, `~/.codex/` and `~/.gemini/antigravity/brain/` every 60 s while the Egon desktop app or standalone mind service is running. Idempotent by external_id.
2. **Push (Claude Code hooks)** — `~/.claude/settings.local.json` runs `scripts/mind_hook.py` on UserPromptSubmit / PostToolUse / Stop. Already active.
3. **Push (MCP server)** — `external/egon_mind_mcp/server.py` is registered as the `egon-mind` MCP tool in Claude Desktop, Antigravity, and Codex. It exposes Context Broker v2 through `mind_context_v2`, keeps `mind_context` as a v2-backed fallback, and includes memory/activity/project/file-lease tools.

## Working in this repo

- The canonical project slug is **`egon`** (per `lib/mind_project_resolver.py`). All sister projects have aliases set up so a Codex session in `Workspace/kms_auto_router` lands under `routster` and finds context from Antigravity's `routster_v3_plan.md`.
- When investigating anything cross-cutting, call `mind_context_v2(project="egon", query=<current request>)` (or the relevant sibling slug) before forming a plan. Use `mind_context` only as fallback for stale tool hosts. Treat the returned capsule, recent activity, relevant memory, audit warnings, and graph insights as ground truth for what other sessions have done.
- When you make a durable decision (architecture choice, gotcha, library evaluation, fix), write it to memory via the hook OR an explicit `mind_memory_upsert` call.
- Every meaningful action must leave shared evidence: prompts/tool use in activity, durable memory for durable outcomes, and file leases before edits when available. Follow `docs/AGENT_ENFORCEMENT.md`, and use `/api/v1/mind/scorecard` plus `/api/v1/mind/enforcement/status` to harden the meta-harness by measured impact.
- The mind dashboard is at the 🌐 **Mind (shared)** tab in Egon's UI. It auto-refreshes every 5 s while Egon is open.

## Hard rules (Bruno, standing)

- **Never delete anything permanently** — back up first.
- **No shell windows on the desktop** — invoke external Python tools via `pythonw.exe` (not `python.exe`).
- **No uncontrolled AI daemons** — heavy KMS routines die with Egon; the lightweight standalone mind service is the explicit exception for cross-agent coordination. No Startup-folder shortcuts or scheduled tasks unless Bruno asks.
- **OSS-first; document everything; visible-first UI.**

See `docs/RECONCILE_2026-05-27.md` for the unified-mind architecture and the full reconcile log.

(Authored by Claude 2026-05-29 as part of unified-mind Phase B.)
