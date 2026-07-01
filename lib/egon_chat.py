"""Egon Chat — a conversational surface backed by a CLOUD model.

Bruno wants a real chat (like claude.ai) inside Egon: he types, the assistant
replies in descriptive text with his mind/vault context, and the conversation
continues. It MUST be cloud-backed — a local LLM on the 8GB box thrashes RAM and
freezes the machine.

Design guarantees:
  • ONE-DIRECTIONAL. The chat injects Egon context as *data* into the prompt and
    replies to Bruno. It never dispatches agents, never calls itself — no loops
    ("don't become schizo originating and receiving at both ends" — Bruno).
  • Provider-agnostic: gemini (default), claude, openai. Keys resolved from env
    then egon-config.json (llm.<provider>_api_key, then llm.api_key). httpx only.
  • Streaming (chat_stream) for real-time, plus a plain chat() fallback.
Bruno 2026-07-01.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable, Iterable

from lib import egon_paths

ROOT = Path(__file__).resolve().parent.parent

# provider -> (default model, env var names to check for the key)
PROVIDERS = {
    "gemini": ("gemini-2.0-flash", ("GEMINI_API_KEY", "GOOGLE_API_KEY")),
    "claude": ("claude-sonnet-5", ("ANTHROPIC_API_KEY", "CLAUDE_API_KEY")),
    "openai": ("gpt-5.5", ("OPENAI_API_KEY", "CHATGPT_API_KEY")),
}
DEFAULT_PROVIDER = "gemini"


def _config() -> dict:
    for p in (ROOT / "egon-config.json", egon_paths.STATE_DIR / "egon-config.json"):
        try:
            if p.exists():
                return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _key_for(provider: str) -> str | None:
    """Resolve a provider's API key: env vars first, then egon-config's llm block
    (llm.<provider>_api_key, llm.<provider>, then the generic llm.api_key)."""
    _default, envs = PROVIDERS[provider]
    for e in envs:
        if os.environ.get(e):
            return os.environ[e]
    llm = (_config().get("llm") or {})
    for k in (f"{provider}_api_key", provider, f"{provider}_key"):
        if llm.get(k):
            return llm[k]
    # generic fallback only if it's clearly this provider's key
    if llm.get("provider", "").lower() == provider and llm.get("api_key"):
        return llm["api_key"]
    return None


def available_providers() -> dict[str, bool]:
    return {p: _key_for(p) is not None for p in PROVIDERS}


def _mind_context(query: str, limit: int = 6) -> str:
    """Top vault hits for the query, as plain text to ground the reply. Pure data
    in — never triggers agents or further LLM calls."""
    try:
        from lib.connection_engine import connect
        hits = connect(query, limit=limit, semantic_search=True, lexical_search=False)
    except Exception:
        return ""
    lines = []
    for h in (hits or [])[:limit]:
        t = (h.get("title") or "").strip()
        s = (h.get("source") or "").strip()
        sn = (h.get("snippet") or "").strip()
        if t:
            lines.append(f"- [{s}] {t}" + (f" — {sn[:120]}" if sn else ""))
    return "\n".join(lines)


_SYSTEM = (
    "You are Egon, Bruno's personal knowledge assistant. You have access to his "
    "unified mind (papers, notes, bookmarks, projects). Answer conversationally "
    "and concretely. When 'Relevant from Bruno's vault' context is provided, use "
    "it and cite sources inline like [zotero]/[paperpile]. You only converse — you "
    "do not dispatch tasks or agents."
)


def _messages_with_context(messages: list[dict], inject_context: bool) -> list[dict]:
    msgs = list(messages)
    if inject_context and msgs and msgs[-1].get("role") == "user":
        ctx = _mind_context(msgs[-1].get("content", ""))
        if ctx:
            msgs.insert(len(msgs) - 1, {
                "role": "user",
                "content": "Relevant from Bruno's vault (context, not a question):\n" + ctx,
            })
    return msgs


# ── Providers (httpx, REST) ──────────────────────────────────────────────────

def _gemini_stream(messages, model, key):
    import httpx
    contents = [{"role": "model" if m["role"] == "assistant" else "user",
                 "parts": [{"text": m["content"]}]} for m in messages]
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:streamGenerateContent?alt=sse&key={key}")
    body = {"contents": contents,
            "systemInstruction": {"parts": [{"text": _SYSTEM}]}}
    with httpx.stream("POST", url, json=body, timeout=120.0) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            try:
                obj = json.loads(line[5:].strip())
                for part in obj["candidates"][0]["content"]["parts"]:
                    if part.get("text"):
                        yield part["text"]
            except Exception:
                continue


def _anthropic_stream(messages, model, key):
    import httpx
    body = {"model": model, "max_tokens": 2048, "system": _SYSTEM,
            "stream": True,
            "messages": [{"role": m["role"], "content": m["content"]}
                         for m in messages if m["role"] in ("user", "assistant")]}
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    with httpx.stream("POST", "https://api.anthropic.com/v1/messages",
                      json=body, headers=headers, timeout=120.0) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line.startswith("data:"):
                continue
            try:
                obj = json.loads(line[5:].strip())
                if obj.get("type") == "content_block_delta":
                    t = obj["delta"].get("text")
                    if t:
                        yield t
            except Exception:
                continue


def _openai_stream(messages, model, key):
    import httpx
    body = {"model": model, "stream": True,
            "messages": [{"role": "system", "content": _SYSTEM}]
                        + [{"role": m["role"], "content": m["content"]} for m in messages]}
    headers = {"Authorization": f"Bearer {key}", "content-type": "application/json"}
    with httpx.stream("POST", "https://api.openai.com/v1/chat/completions",
                      json=body, headers=headers, timeout=120.0) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line.startswith("data:") or line.strip() == "data: [DONE]":
                continue
            try:
                obj = json.loads(line[5:].strip())
                t = obj["choices"][0]["delta"].get("content")
                if t:
                    yield t
            except Exception:
                continue


_STREAMERS = {"gemini": _gemini_stream, "claude": _anthropic_stream, "openai": _openai_stream}


def stream_chat(messages: list[dict], provider: str = DEFAULT_PROVIDER,
                model: str | None = None, inject_context: bool = True) -> Iterable[str]:
    """Yield response text chunks in real time. Raises if the provider has no key."""
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider {provider}")
    key = _key_for(provider)
    if not key:
        raise RuntimeError(
            f"no API key for {provider}. Add llm.{provider}_api_key to egon-config.json "
            f"or set {PROVIDERS[provider][1][0]}.")
    model = model or PROVIDERS[provider][0]
    msgs = _messages_with_context(messages, inject_context)
    yield from _STREAMERS[provider](msgs, model, key)


def chat(messages: list[dict], provider: str = DEFAULT_PROVIDER,
         model: str | None = None, inject_context: bool = True) -> str:
    """Non-streaming: collect the full reply."""
    return "".join(stream_chat(messages, provider, model, inject_context))


if __name__ == "__main__":
    print("providers with keys:", available_providers())
    import sys
    prov = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PROVIDER
    if _key_for(prov):
        print(f"--- {prov} test ---")
        for chunk in stream_chat([{"role": "user", "content": "In one sentence, what are you?"}],
                                 provider=prov, inject_context=False):
            print(chunk, end="", flush=True)
        print()
    else:
        print(f"no key for {prov}; set it in egon-config.json llm.{prov}_api_key")
