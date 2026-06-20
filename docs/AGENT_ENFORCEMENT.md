# Egon Agent Enforcement Contract

This is the shared operating contract for Claude Code, Codex, Antigravity,
ChatGPT, Hermes Agent, and future AI bodies working inside Bruno's Personal OS.

## Non-Negotiable Startup

At the start of every session:

1. Resolve the canonical project slug for the current workspace using Egon's
   resolver rules and aliases.
2. Call `mind_context_v2(project=<slug>, query=<current request>)` through the
   Egon mind MCP tool, or the REST endpoint at
   `http://127.0.0.1:8000/api/v1/mind/context/v2`. If the tool host only
   exposes `mind_context`, use it as the fallback; the patched MCP server routes
   it through Context Broker v2.
3. Treat the returned capsule, recent activity, active sessions, file leases,
   structural insights, audit warnings, and relevant memory as authoritative
   project history.
4. If the mind is offline, start the standalone mind service if possible. If it
   still fails, tell Bruno once and continue with extra caution.

## Documentation Rule

Every meaningful action must leave shared evidence.

- Every user prompt and tool action should become `activity` in the mind. Hooks
  or ingestion may do this automatically; if they are unavailable, the agent
  must write a compact manual activity/memory note.
- Every durable outcome must become `memory`: architecture decisions, fixes,
  failure modes, gotchas, user preferences, setup changes, verification results,
  and unresolved risks.
- Durable memory should be compact but useful. Include: what changed, why it
  matters, affected files/endpoints, verification performed, and remaining risk.
- Do not put noisy step-by-step scratch into durable memory. Full transcripts
  can be ingested separately; durable memory is for future retrieval.

## Structural Insight Rule

The mind also turns agent actions and artifact interactions into a typed graph:
agents, sessions, projects, tools, memories, tags, files, URLs, API endpoints,
category objects, and morphisms. Use `/api/v1/mind/graph` or the structural
insights returned by `mind_context_v2` when Bruno is searching, asking for
connections, or trying to understand why a project feels stuck.

The graph writes Gephi-compatible `.gexf` artifacts under
`state/mind_graph/`. Treat those artifacts as maps for discovery, not final
truth: inspect surprising links before acting on them.

## Audit Rule

Use `/api/v1/mind/audit` to check whether recent sessions actually obeyed this
contract. The audit flags sessions with missing context calls, missing activity,
edit/write tools without lease evidence, missing durable memory or summaries,
stale active sessions, and unreleased file leases.

Passing the audit is the evidence that the shared mind is being used. If a
session is flagged, fix the underlying hook/config/tool behavior instead of
trusting prompts alone.

Use `/api/v1/mind/scorecard` and `/api/v1/mind/enforcement/status` to quantify
meta-harness health, Context Broker v2 adoption, token ROI, file lease coverage,
and configuration drift. These endpoints are the operational truth for whether
the harness is getting cheaper and more reliable with use.

## Coordination Rule

Before editing a file, acquire or respect a file lease when the tool surface
supports it. If leases are unavailable, check recent activity for overlapping
work and mention the uncertainty.

## Public Release Gate

No AI body may create a public repository, make a repository public, push to a
public remote, publish a release, or otherwise expose code publicly until a
privacy and security release gate has passed and been documented.

The gate must check the full tree, staged diff, reachable Git history,
generated files, documentation, examples, build artifacts, and release assets
for:

- PII, local machine paths, usernames, and workspace metadata.
- Secrets, tokens, credentials, private keys, OAuth/client IDs, and environment
  files.
- Internal infrastructure names, local-only URLs, private network details, and
  agent memory or session artifacts.
- Dependency hazards, client-side vulnerabilities, unsafe HTML injection,
  over-broad service-worker behavior, and any other security issue.

If the gate has not passed, the remote must remain private or absent. If public
exposure happens accidentally, immediately make the repo private or delete it,
sanitize and rewrite history, prune/remediate local artifacts and remotes,
write an incident memory, and require Bruno's explicit approval before any
future public exposure.

## Token Discipline

Use the mind to reduce token waste:

- Prefer targeted `mind_context_v2(project=<slug>, query=<task>,
  budget_chars=<small useful budget>)` over loading broad history.
- Summarize retrieved context before acting.
- Do not re-investigate facts already established in durable memory unless they
  are stale or suspicious.
- Write concise durable memories so future sessions can avoid rediscovery.
- When per-turn usage is available, log it through `/api/v1/mind/ledger/turns`
  so token ROI can move from estimate to measurement.

## Token-Loss Incident Guard

Agent state is user state. A broken Claude/Codex/Antigravity session list can
burn an entire quota period by forcing rediscovery, retries, and blind work.

- No Egon routine may rename, delete, move, or otherwise hide live agent
  transcripts or session metadata. Claude Code live transcripts must remain as
  `*.jsonl`; archival copies may be created beside them, but the live path must
  stay readable.
- Any routine that touches agent-owned state must create a restore point first.
  For Claude transcript maintenance, `scripts/compact_transcripts.py` must use
  `lib.agent_state_guard.create_agent_restore_point` and refuse to proceed if
  requested files were not captured.
- `/api/v1/mind/enforcement/status` is the operational gate. It must check
  Claude session-state health, agent-state restore guards, live MCP
  `mind_context_v2`, context coverage, Context Broker v2 adoption, and token ROI.
- If `claude_session_state`, `mcp_live_smoke`, `agent_state_guard`, or
  `token_waste_sentinel` fails, stop long-running agent work and fix the harness
  before spending more tokens.

## Standard Prompt

Use this when an AI body needs a manual nudge:

```
Before answering, use Egon's unified mind. Resolve this workspace's canonical
project slug, call mind_context(project=<slug>, query=<my request>), treat the
project slug, call mind_context_v2(project=<slug>, query=<my request>) or fall
back to mind_context, treat the returned capsule/activity/memory as
authoritative project history, check active file leases before edits, and after
meaningful actions write compact durable memory covering what changed, why,
files/endpoints, verification, and remaining risks. Keep context retrieval
targeted to save tokens, and check /api/v1/mind/scorecard when deciding what to
harden next.
```
