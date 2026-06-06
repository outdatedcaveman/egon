# Egon Mind MCP server

A Model Context Protocol server that exposes Egon's unified mind as
MCP tools. Works in Claude Desktop, Antigravity (Gemini IDE), Codex
CLI, Cursor, Goose, and any MCP-capable agent.

## What you get

8 tools, all backed by Egon's local `/api/v1/mind/*` REST surface:

- `mind_stats` — counts + 24 h rollups
- `mind_context` — recent activity + relevant memory for a project/query
- `mind_activity_list` — filter activity rows
- `mind_activity_append` — log new activity
- `mind_memory_search` — search durable memory
- `mind_memory_upsert` — write durable memory
- `mind_projects_list` — list projects
- `mind_register_agent` — register self/other as an agent

## Transport

stdio JSON-RPC 2.0. The server makes HTTP calls to Egon's Panop on
`http://127.0.0.1:8000` (override via `EGON_MIND_API` env var).
Stdlib-only — runs under any Python 3.8+, no venv required.

## Install in Claude Desktop

Add to `%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "egon-mind": {
      "command": "python.exe",
      "args": ["C:/Users/bruno/Claude Code/egon/external/egon_mind_mcp/server.py"]
    }
  }
}
```

Restart Claude Desktop. New Cowork sessions can call the tools.

## Install in Antigravity (Gemini IDE)

Add to `%USERPROFILE%\.gemini\antigravity-ide\mcp_config.json`:

```json
{
  "mcpServers": {
    "egon-mind": {
      "command": "python.exe",
      "args": ["C:/Users/bruno/Claude Code/egon/external/egon_mind_mcp/server.py"]
    }
  }
}
```

Restart Antigravity. Tools appear under the MCP menu.

## Install in Codex

Append to `%USERPROFILE%\.codex\config.toml`:

```toml
[mcp_servers.egon_mind]
command = 'python.exe'
args = ['C:/Users/bruno/Claude Code/egon/external/egon_mind_mcp/server.py']
startup_timeout_sec = 30
```

New Codex sessions pick it up automatically.

## How it integrates with Egon's other layers

- **Pull-based ingestion** (`lib/mind_ingest.py`) — Egon polls each
  agent's local memory dir every 60 s while open. That gets you READ.
- **Push-based hooks** (`scripts/mind_hook.py`) — Claude Code hooks
  POST activity in real time. That gets you WRITE for Claude Code.
- **This MCP server** — gives WRITE access to every MCP-capable agent
  (Claude Desktop, Antigravity, Codex, Cursor, …). Models can choose
  to call `mind_memory_upsert` whenever something durable was learned,
  or `mind_context` to inherit knowledge from other sessions.

Three independent layers; redundant on purpose. If one fails the
others keep the mind populated.
