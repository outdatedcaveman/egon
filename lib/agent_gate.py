"""Agent gate — vetted external agents talk to Egon through here.

Status: SCAFFOLD ONLY (no endpoint exposed yet).

Design:
- Egon binds 127.0.0.1 only. No external network access by default.
- When you eventually wire other agents (Claude API, OpenAI, local LLMs) to drive
  Egon, they hit a separate FastAPI endpoint /agent that:
    1. Requires a per-agent token from `egon-config.json["agent_allowlist"]`.
    2. Verifies the token via constant-time HMAC compare.
    3. Logs every call to `logs/agent-<YYYY-MM>.jsonl` with caller id + intent.
    4. Routes read-only by default; writes need an explicit `confirm_token`
       that the user signs in the UI within 60s of the request.

To add an agent to the allowlist (future):
    egon-config.json:
      {
        "agent_allowlist": {
          "<agent-id>": {
            "token": "<bcrypt-hash>",
            "scopes": ["read.ledger", "read.inbox"],
            "added_at": "..."
          }
        }
      }

For now this file documents the contract — DO NOT enable any inbound endpoint
without (a) UI flow for token grants and (b) the user explicitly toggling
`enable_agent_endpoint: true` in egon-config.json.
"""
from __future__ import annotations

ALLOWED_SCOPES = {
    "read.ledger", "read.inbox", "read.references", "read.media",
    "read.artifacts", "read.databases",
    "write.inbox.classify", "write.references.add",
    "write.bookmarks.add",
}


def is_endpoint_enabled() -> bool:
    """Always False until the UI flow exists. Hard guard."""
    return False
