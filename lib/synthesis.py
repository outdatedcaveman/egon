"""Synthesis — turn Connect's retrieved links into an actual answer.

Bruno 2026-06-12, strategy item #2 ("close the loop from retrieval to
answers"): the Connection Engine surfaces *what* in your archives relates to
what you're writing; this layer says *so what* — how the pieces connect, where
they agree or contradict you, and what's worth opening first.

Brain: the locally-decided default from claude-meta/.env — Ollama qwen2.5:3b
on http://localhost:11434/v1 (OpenAI-compatible). $0, fully private, sized for
this machine's 8 GB RAM (the model loads on demand and auto-unloads when idle).
Provider-agnostic by design: LLM_ENDPOINT / LLM_MODEL / LLM_API_KEY are read
from claude-meta/.env and can be overridden by EGON_SYNTH_* env vars — point
them at Headroom (:8787/v1) + a cloud key later for a smarter brain without
touching this code.

Token discipline: synthesis runs ONLY on an explicit user action (the
🧠 button) — never automatically. If the endpoint is down, callers get
{"status":"unavailable"} and surfaces fall back to connections-only.
"""
from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = Path(r"C:/Users/bruno/Claude Code/claude-meta/.env")

_DEFAULTS = {
    "endpoint": "http://localhost:11434/v1",
    "model": "qwen2.5:3b",
    "api_key": "ollama",          # ollama ignores it; OpenAI-compat needs one
}


def _config() -> dict:
    cfg = dict(_DEFAULTS)
    try:
        if ENV_PATH.exists():
            for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("LLM_ENDPOINT="):
                    cfg["endpoint"] = line.split("=", 1)[1].strip()
                elif line.startswith("LLM_MODEL="):
                    cfg["model"] = line.split("=", 1)[1].strip()
                elif line.startswith("LLM_API_KEY="):
                    v = line.split("=", 1)[1].strip()
                    if v:
                        cfg["api_key"] = v
    except Exception:
        pass
    # Explicit overrides win (e.g. route via Headroom + cloud model later).
    cfg["endpoint"] = os.environ.get("EGON_SYNTH_ENDPOINT", cfg["endpoint"]).rstrip("/")
    cfg["model"] = os.environ.get("EGON_SYNTH_MODEL", cfg["model"])
    cfg["api_key"] = os.environ.get("EGON_SYNTH_API_KEY", cfg["api_key"])
    return cfg


def available(timeout: float = 2.5) -> bool:
    cfg = _config()
    try:
        base = cfg["endpoint"].rsplit("/v1", 1)[0]
        with urllib.request.urlopen(base + "/api/tags" if "11434" in base
                                    else cfg["endpoint"] + "/models",
                                    timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _chat(prompt: str, cfg: dict, max_tokens: int = 380,
          timeout: float = 90.0) -> str | None:
    body = json.dumps({
        "model": cfg["model"],
        "max_tokens": max_tokens,
        "temperature": 0.4,
        "messages": [
            {"role": "system", "content":
                "You are Egon, Bruno's personal knowledge assistant. You know "
                "his archives. Be concrete and brief; never invent sources — "
                "only discuss the provided items."},
            {"role": "user", "content": prompt},
        ],
    }).encode()
    req = urllib.request.Request(
        cfg["endpoint"] + "/chat/completions", data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {cfg['api_key']}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
        return (data.get("choices") or [{}])[0].get("message", {}).get("content")
    except Exception:
        return None


def synthesize(text: str, connections: list[dict],
               max_items: int = 10) -> dict[str, Any]:
    """One short, grounded insight: how the retrieved items bear on `text`."""
    text = (text or "").strip()
    if not text:
        return {"status": "error", "error": "no input text"}
    cfg = _config()
    items = []
    for i, c in enumerate(connections[:max_items], 1):
        items.append(f"{i}. [{c.get('source','?')}] {c.get('title','')}"
                     + (f" — {c.get('snippet','')[:110]}" if c.get("snippet") else ""))
    if not items:
        return {"status": "error", "error": "no connections to synthesize"}

    prompt = (
        "Bruno is currently writing/reading this:\n---\n" + text[:1800] +
        "\n---\nThese items from HIS OWN archives were retrieved as related:\n"
        + "\n".join(items) +
        "\n\nIn at most 120 words, tell him: (a) the single strongest "
        "connection and why it matters for what he's writing, (b) any tension "
        "or contradiction between his text and the items, (c) which ONE item "
        "to open first. Refer to items by their number and name. No preamble.")

    # RAM-aware model chain — this is an 8 GB machine; trying a model that
    # doesn't fit doesn't fail fast, it PAGES for minutes ("unable to allocate
    # CPU buffer" at best, swap-storm at worst). So we measure free RAM first
    # and enter the chain at the largest model that actually fits, falling
    # through on any failure. An answer from 1.5b/0.5b beats no answer.
    # Bruno 2026-06-12.
    free_gb = _free_ram_gb()
    chain_all = [(cfg["model"], 3.0), ("qwen2.5:1.5b", 1.6), ("qwen2.5:0.5b", 0.8)]
    seen, chain = set(), []
    for model, need in chain_all:
        if model not in seen:
            seen.add(model)
            chain.append((model, need))
    tried = []
    for model, need_gb in chain:
        if free_gb is not None and free_gb < need_gb:
            tried.append(f"{model}(skipped, {free_gb:.1f}GB free)")
            continue
        out = _chat(prompt, dict(cfg, model=model), timeout=75.0)
        if out:
            return {"status": "ok", "model": model, "insight": out.strip(),
                    "free_ram_gb": free_gb,
                    **({"degraded_from": cfg["model"]} if model != cfg["model"] else {})}
        tried.append(model)
    # Last resort: smallest model regardless of the RAM estimate.
    out = _chat(prompt, dict(cfg, model="qwen2.5:0.5b"), timeout=75.0)
    if out:
        return {"status": "ok", "model": "qwen2.5:0.5b", "insight": out.strip(),
                "degraded_from": cfg["model"], "free_ram_gb": free_gb}
    return {"status": "unavailable",
            "error": f"no synthesis model could run (tried {', '.join(tried)} @ {cfg['endpoint']})"}


def _free_ram_gb() -> float | None:
    try:
        import ctypes
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        st = MEMORYSTATUSEX(); st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
        return st.ullAvailPhys / (1024 ** 3)
    except Exception:
        return None
