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

import base64
import json
import mimetypes
import os
import re
import time
from pathlib import Path
from typing import Callable, Iterable

from lib import egon_paths

ROOT = Path(__file__).resolve().parent.parent

# provider -> model list (top-tier first), default model, env var names for the key.
# Default = the STRONGEST model of each provider: this is Bruno's primary
# high-quality work surface, meant to match/beat the native apps (2026-07-01).
PROVIDERS = {
    "gemini": {
        "models": ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash"],
        "default": "gemini-2.5-pro",
        "envs": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        "vision": True,
    },
    "claude": {
        "models": ["claude-opus-4-8", "claude-sonnet-5", "claude-haiku-4-5-20251001"],
        "default": "claude-opus-4-8",
        "envs": ("ANTHROPIC_API_KEY", "CLAUDE_API_KEY"),
        "vision": True,
    },
    "openai": {
        "models": ["gpt-5.5", "gpt-5", "gpt-4.1"],
        "default": "gpt-5.5",
        "envs": ("OPENAI_API_KEY", "CHATGPT_API_KEY"),
        "vision": True,
    },
}
DEFAULT_PROVIDER = "gemini"


def default_model(provider: str) -> str:
    return PROVIDERS[provider]["default"]


def models_for(provider: str) -> list[str]:
    return list(PROVIDERS[provider]["models"])


def _envs(provider: str) -> tuple:
    return PROVIDERS[provider]["envs"]


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
    for e in _envs(provider):
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


# Slugs that are also common English words — only treat as a project when the
# broader message context makes it a clear reference (avoid false positives).
_AMBIGUOUS_SLUGS = {"double", "flood"}


def _detect_project(text: str) -> str | None:
    """If the message names one of Bruno's projects (mouseion, egon, routster…),
    return its canonical slug so we can pull that project's cross-agent capsule."""
    try:
        from lib.mind_project_resolver import known_project_slugs
        known = known_project_slugs()
    except Exception:
        return None
    toks = set(re.findall(r"[a-z0-9_\-]{3,}", (text or "").lower()))
    hits = [s for s in known if s in toks]
    clear = [h for h in hits if h not in _AMBIGUOUS_SLUGS]
    picks = clear or hits
    # Longest match wins (more specific), stable for repeatability.
    return sorted(picks, key=len, reverse=True)[0] if picks else None


def _canonical_context(project: str, max_sessions: int = 5) -> str:
    """The canonical view of a project: every AI's sessions that Egon's own
    content classifier filed under it (canonical_assignments), newest first,
    with their goal summaries. This is the consolidated-mind ground truth —
    independent of which app or folder the work happened in."""
    import sqlite3
    try:
        from lib.mind_context_broker import DB_PATH
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=4)
        conn.row_factory = sqlite3.Row
    except Exception:
        return ""
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM canonical_assignments "
            "WHERE item_type='session' AND canonical_project=?", (project,)).fetchone()[0]
        if not total:
            return ""
        rows = conn.execute(
            """SELECT s.external_id, s.started_at, s.summary, a.name AS agent
               FROM canonical_assignments ca
               JOIN sessions s ON s.id = CAST(ca.item_id AS INTEGER)
               LEFT JOIN agents a ON a.id = s.agent_id
               WHERE ca.item_type='session' AND ca.canonical_project=?
                 AND s.summary IS NOT NULL AND s.summary != ''
               ORDER BY s.started_at DESC LIMIT ?""",
            (project, max_sessions)).fetchall()
    except Exception:
        return ""
    finally:
        conn.close()
    if not rows:
        return ""
    lines = [f"CANONICAL PROJECT '{project}' — {total} sessions across your AIs "
             f"(filed by Egon's content classifier); the most recent:"]
    for r in rows:
        agent = (r["agent"] or "ai").split(":")[0]
        when = time.strftime("%Y-%m-%d", time.localtime(r["started_at"] or 0))
        head = " ".join((r["summary"] or "").split())[:500]
        lines.append(f"- [{agent} {when}] {head}")
    return "\n".join(lines)


def _mind_context(query: str, limit: int = 6) -> str:
    """Assemble grounding context for the reply. Two layers, both pure DATA in
    (never dispatches agents or makes further LLM calls):

      1. The SHARED-MIND CAPSULE (lib.mind_context_broker) — the real answer to
         'does it have context on everything from the three AIs': a project-aware
         digest of durable memory, recent activity, and structural insights
         pulled from Claude, Codex, and Antigravity's unified mind.
      2. ARCHIVE hits (connect()) — Zotero, Paperpile, Drive, Kindle, bookmarks…
    """
    parts: list[str] = []
    project = _detect_project(query)

    # 1) cross-agent capsule (project-aware when the message names a project)
    try:
        from lib.mind_context_broker import build_context_capsule
        cap = build_context_capsule(
            project=project, query=query, budget_chars=3500,
            limit_activity=6, limit_memory=6,
            include_graph=True, include_audit=False)
        if isinstance(cap, dict) and cap.get("status") == "ok":
            briefing = (cap.get("briefing") or "").strip()
            if briefing:
                parts.append(briefing)
    except Exception:
        pass

    # 1b) CANONICAL project context — the primary source (Bruno 2026-07-01: the
    # consolidated mind, not the messy app repos, is where projects live). Pull
    # the sessions Egon's own classifier filed under this project, across ALL
    # AIs, with their goal summaries. Read-only, no lock contention.
    if project:
        try:
            canon = _canonical_context(project)
            if canon:
                parts.append(canon)
        except Exception:
            pass

    # 1c) ACTUAL repo source — parameter-level code access. Secondary to the
    # canonical mind but still valuable: the CURRENT state of the code on disk.
    try:
        from lib import repo_map
        files = repo_map.repo_files_for(project, query, max_files=4)
        if files:
            blocks = [f"### {f['repo']}/{f['path']}\n{f['snippet']}" for f in files]
            parts.append("From your project repo (actual current source):\n"
                         + "\n\n".join(blocks))
    except Exception:
        pass

    # 2) archive/vault hits via the Connection Engine
    try:
        from lib.connection_engine import connect
        res = connect(query, limit=limit, semantic_search=True, lexical_search=False)
        hits = res.get("connections") if isinstance(res, dict) else (res or [])
        lines = []
        for h in (hits or [])[:limit]:
            if not isinstance(h, dict):
                continue
            t = (h.get("title") or "").strip()
            s = (h.get("source") or "").strip()
            why = h.get("why")
            sn = (h.get("snippet") or "").strip()
            if not sn and isinstance(why, (list, tuple)):
                sn = ", ".join(str(w) for w in why[:5])
            if t:
                lines.append(f"- [{s}] {t[:110]}" + (f" — {sn[:120]}" if sn else ""))
        if lines:
            parts.append("Relevant items from your archives:\n" + "\n".join(lines))
    except Exception:
        pass

    return "\n\n".join(parts)


_SYSTEM = (
    "You are Egon, Bruno's personal knowledge assistant and the memory shared by "
    "his three coding AIs (Claude Code, Codex, Antigravity). You DO have running "
    "context on his projects and work: each turn you are given an EGON SHARED-MIND "
    "CAPSULE — a digest of durable memory, recent cross-agent activity, structural "
    "insights, and archive matches drawn from that unified mind. TREAT THE CAPSULE "
    "AS YOUR OWN KNOWLEDGE: when it names a project (e.g. mouseion, egon, routster, "
    "panop), speak about it directly from the capsule instead of claiming you don't "
    "know what it is. If the capsule is genuinely thin on a detail, say what you DO "
    "have and name the specific gap — don't ask the user to explain their own "
    "project from scratch. You are ALSO given, when relevant, ACTUAL SOURCE from "
    "his project repos (real functions, parameters, config) — reason at that "
    "concrete level: reference real symbols and file paths, not vague guesses. "
    "Bruno may attach images and documents; read them directly. Answer "
    "conversationally and concretely; this is his primary work surface, so match "
    "the depth and quality of a native AI app. Cite sources inline like "
    "[zotero]/[paperpile]/[memory 1539]/[repo: path]. You only converse — you "
    "never dispatch tasks or agents."
)


def _text_of(content) -> str:
    """Flatten a message's content (str or multimodal parts) to text, for
    retrieval queries. Images contribute nothing here; documents contribute
    their extracted text."""
    return " ".join(
        (p.get("text", "") if p.get("type") in ("text", "document") else "")
        for p in _normalize_parts(content)
    ).strip()


def _messages_with_context(messages: list[dict], inject_context: bool) -> list[dict]:
    msgs = list(messages)
    if inject_context and msgs and msgs[-1].get("role") == "user":
        ctx = _mind_context(_text_of(msgs[-1].get("content", "")))
        if ctx:
            msgs.insert(len(msgs) - 1, {
                "role": "user",
                "content": ("EGON SHARED-MIND CAPSULE (your own memory across Claude, "
                            "Codex, Antigravity + Bruno's archives — context, not a "
                            "question):\n" + ctx),
            })
    return msgs


# ── Multimodal message format ────────────────────────────────────────────────
# A message is {"role": "user"|"assistant", "content": <str> | <list of parts>}.
# A part is one of:
#   {"type": "text",     "text": "..."}
#   {"type": "image",    "mime": "image/png", "data": "<base64>"}
#   {"type": "document", "name": "paper.pdf", "text": "<extracted text>"}
# Each provider adapter converts these to its own wire shape. Documents are
# injected as text (universal, works on every provider); images ride natively.

def _normalize_parts(content) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        out = []
        for p in content:
            if isinstance(p, str):
                out.append({"type": "text", "text": p})
            elif isinstance(p, dict) and p.get("type"):
                out.append(p)
        return out
    return [{"type": "text", "text": str(content or "")}]


def _doc_as_text(p: dict) -> str:
    name = p.get("name") or "document"
    return f"[Attached document: {name}]\n{p.get('text') or ''}"


_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_AUDIO_EXT = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac", ".opus"}
_VIDEO_EXT = {".mp4", ".mov", ".webm", ".avi", ".mkv", ".3gp"}
_MAX_DOC_CHARS = 24000
_MAX_MEDIA_BYTES = 20 * 1024 * 1024   # inline base64 ceiling (Gemini inlineData)


def attach_from_path(path: str) -> dict | None:
    """Turn a file on disk into a message part. Images → base64 image part;
    audio/video → base64 media parts (Gemini consumes them natively; other
    providers get an honest 'switch to Gemini' note); PDFs/office/text/code →
    extracted-text document part. Returns None if the file can't be read."""
    p = Path(path)
    if not p.exists() or not p.is_file():
        return None
    ext = p.suffix.lower()
    if ext in _IMAGE_EXT or ext in _AUDIO_EXT or ext in _VIDEO_EXT:
        try:
            if p.stat().st_size > _MAX_MEDIA_BYTES and ext not in _IMAGE_EXT:
                return {"type": "document", "name": p.name,
                        "text": f"(media file too large to inline: {p.stat().st_size >> 20}MB; "
                                f"max {_MAX_MEDIA_BYTES >> 20}MB)"}
            data = base64.b64encode(p.read_bytes()).decode("ascii")
        except Exception:
            return None
        mime = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
        kind = ("image" if ext in _IMAGE_EXT
                else "audio" if ext in _AUDIO_EXT else "video")
        return {"type": kind, "mime": mime, "data": data, "name": p.name}
    text = _extract_text(p, ext)
    if text is None:
        return None
    return {"type": "document", "name": p.name, "text": text[:_MAX_DOC_CHARS]}


def _extract_text(p: Path, ext: str) -> str | None:
    if ext == ".pdf":
        # Reuse the same fast extractor the hydration worker uses.
        try:
            import fitz  # PyMuPDF
            doc = fitz.open(str(p))
            out = []
            for page in doc:
                out.append(page.get_text())
                if sum(len(x) for x in out) > _MAX_DOC_CHARS:
                    break
            doc.close()
            return "\n".join(out)
        except Exception:
            pass
        try:
            from pypdf import PdfReader
            r = PdfReader(str(p))
            return "\n".join((pg.extract_text() or "") for pg in r.pages[:40])
        except Exception:
            return None
    if ext == ".docx":
        try:
            import docx  # python-docx
            d = docx.Document(str(p))
            return "\n".join(par.text for par in d.paragraphs)
        except Exception:
            return None
    # plain text / code / data — read directly
    try:
        return p.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None


# ── Providers (httpx, REST) ──────────────────────────────────────────────────

def _gemini_stream(messages, model, key, params):
    import httpx
    contents = []
    for m in messages:
        role = "model" if m["role"] == "assistant" else "user"
        gparts = []
        for p in _normalize_parts(m["content"]):
            if p["type"] in ("image", "audio", "video") and p.get("data"):
                # Gemini consumes image/audio/video natively via inlineData
                gparts.append({"inlineData": {"mimeType": p.get("mime", "application/octet-stream"),
                                              "data": p["data"]}})
            elif p["type"] == "document":
                gparts.append({"text": _doc_as_text(p)})
            else:
                gparts.append({"text": p.get("text", "")})
        contents.append({"role": role, "parts": gparts})
    gen = {}
    if params.get("temperature") is not None:
        gen["temperature"] = params["temperature"]
    if params.get("max_tokens"):
        gen["maxOutputTokens"] = params["max_tokens"]
    body = {"contents": contents,
            "systemInstruction": {"parts": [{"text": params.get("system") or _SYSTEM}]}}
    if gen:
        body["generationConfig"] = gen
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:streamGenerateContent?alt=sse&key={key}")
    with httpx.stream("POST", url, json=body, timeout=180.0) as r:
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


def _anthropic_stream(messages, model, key, params):
    import httpx
    conv = []
    for m in messages:
        if m["role"] not in ("user", "assistant"):
            continue
        blocks = []
        for p in _normalize_parts(m["content"]):
            if p["type"] == "image" and p.get("data"):
                blocks.append({"type": "image", "source": {
                    "type": "base64", "media_type": p.get("mime", "image/png"),
                    "data": p["data"]}})
            elif p["type"] in ("audio", "video"):
                blocks.append({"type": "text", "text":
                               f"[Attached {p['type']} '{p.get('name','file')}' — this "
                               "provider can't consume it; switch to Gemini for audio/video.]"})
            elif p["type"] == "document":
                blocks.append({"type": "text", "text": _doc_as_text(p)})
            else:
                blocks.append({"type": "text", "text": p.get("text", "")})
        conv.append({"role": m["role"], "content": blocks})
    body = {"model": model, "max_tokens": params.get("max_tokens") or 4096,
            "system": params.get("system") or _SYSTEM, "stream": True,
            "messages": conv}
    if params.get("temperature") is not None:
        body["temperature"] = params["temperature"]
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    with httpx.stream("POST", "https://api.anthropic.com/v1/messages",
                      json=body, headers=headers, timeout=180.0) as r:
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


def _openai_stream(messages, model, key, params):
    import httpx
    conv = [{"role": "system", "content": params.get("system") or _SYSTEM}]
    for m in messages:
        parts = _normalize_parts(m["content"])
        # plain text → string content (cheaper); mixed → content array
        if all(p["type"] in ("text", "document") for p in parts):
            text = "\n".join(p.get("text", "") if p["type"] == "text" else _doc_as_text(p)
                             for p in parts)
            conv.append({"role": m["role"], "content": text})
        else:
            arr = []
            for p in parts:
                if p["type"] == "image" and p.get("data"):
                    arr.append({"type": "image_url", "image_url": {
                        "url": f"data:{p.get('mime','image/png')};base64,{p['data']}"}})
                elif p["type"] in ("audio", "video"):
                    arr.append({"type": "text", "text":
                                f"[Attached {p['type']} '{p.get('name','file')}' — this "
                                "provider can't consume it; switch to Gemini for audio/video.]"})
                elif p["type"] == "document":
                    arr.append({"type": "text", "text": _doc_as_text(p)})
                else:
                    arr.append({"type": "text", "text": p.get("text", "")})
            conv.append({"role": m["role"], "content": arr})
    body = {"model": model, "stream": True, "messages": conv}
    if params.get("temperature") is not None:
        body["temperature"] = params["temperature"]
    if params.get("max_tokens"):
        body["max_completion_tokens"] = params["max_tokens"]
    headers = {"Authorization": f"Bearer {key}", "content-type": "application/json"}
    with httpx.stream("POST", "https://api.openai.com/v1/chat/completions",
                      json=body, headers=headers, timeout=180.0) as r:
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


def _is_transient(err: Exception) -> bool:
    """Capacity/quota errors we can retry on a cheaper model of the same provider
    (e.g. gemini-2.5-pro 429s on the free tier → fall to gemini-2.5-flash)."""
    s = str(err).lower()
    return any(code in s for code in ("429", "503", "500", "overloaded",
                                      "capacity", "quota", "rate limit",
                                      "resource_exhausted", "unavailable"))


def _fallback_chain(provider: str, model: str) -> list[str]:
    """The chosen model first, then the provider's cheaper models as fallbacks."""
    chain = [model]
    for m in models_for(provider):
        if m not in chain:
            chain.append(m)
    return chain


def stream_chat(messages: list[dict], provider: str = DEFAULT_PROVIDER,
                model: str | None = None, inject_context: bool = True,
                temperature: float | None = None, max_tokens: int | None = None,
                system: str | None = None) -> Iterable[str]:
    """Yield response text chunks in real time. Supports multimodal `content`
    (see the format note above) and per-call parameters. If the chosen model is
    capacity-limited (e.g. gemini-2.5-pro 429 on the free tier) it transparently
    falls back to the next model of the SAME provider. Raises if no key."""
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider {provider}")
    key = _key_for(provider)
    if not key:
        raise RuntimeError(
            f"no API key for {provider}. Add llm.{provider}_api_key to egon-config.json "
            f"or set {_envs(provider)[0]}.")
    params = {"temperature": temperature, "max_tokens": max_tokens, "system": system}
    msgs = _messages_with_context(messages, inject_context)

    chain = _fallback_chain(provider, model or default_model(provider))
    last_err: Exception | None = None
    for i, mdl in enumerate(chain):
        gen = _STREAMERS[provider](msgs, mdl, key, params)
        try:
            first = next(gen)             # triggers the request; may raise here
        except StopIteration:
            return                         # clean empty response
        except Exception as e:             # noqa: BLE001
            last_err = e
            if _is_transient(e) and i < len(chain) - 1:
                continue                   # try the next (cheaper) model
            raise
        yield first
        yield from gen
        return
    if last_err:
        raise last_err


def chat(messages: list[dict], provider: str = DEFAULT_PROVIDER,
         model: str | None = None, inject_context: bool = True, **kw) -> str:
    """Non-streaming: collect the full reply."""
    return "".join(stream_chat(messages, provider, model, inject_context, **kw))


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
