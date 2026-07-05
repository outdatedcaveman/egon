"""Push notifications that reach Bruno ANYWHERE — improvement #2, 2026-07-04.

The adb nudge only works on home WiFi. This publishes to ntfy.sh (free pub/sub
push): Bruno subscribes once to a secret random topic in the ntfy Android app,
and Egon's pushes arrive on the street too.

PRIVACY (masterlaw — no PII/content leak through third parties): messages are
GENERIC — counts, goal ids, percentages. Task descriptions, file names, chat
content NEVER ride in a push; the details live in Egon, the push just says
"come look". The topic is a 24-hex secret stored in gitignored egon-config.json
(knowing it = receiving the pushes, so it's treated like a token).
"""
from __future__ import annotations

import json
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = ROOT / "egon-config.json"
SERVER = "https://ntfy.sh"


def topic() -> str:
    """Read (or create once) the secret push topic in egon-config.json."""
    cfg = {}
    try:
        cfg = json.loads(CFG.read_text(encoding="utf-8"))
    except Exception:
        pass
    t = ((cfg.get("push") or {}).get("ntfy_topic") or "").strip()
    if t:
        return t
    t = "egon-" + secrets.token_hex(12)
    cfg.setdefault("push", {})["ntfy_topic"] = t
    try:
        CFG.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception:
        pass
    return t


def subscribe_url() -> str:
    return f"{SERVER}/{topic()}"


def push(title: str, message: str, priority: int = 3, tags: str = "robot") -> bool:
    """Best-effort publish; never raises. Keep title/message GENERIC."""
    try:
        import httpx
        r = httpx.post(subscribe_url(), content=message.encode("utf-8"),
                       headers={"Title": title[:120], "Priority": str(priority),
                                "Tags": tags},
                       timeout=6)
        return r.status_code < 400
    except Exception:
        return False
