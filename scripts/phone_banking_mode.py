"""Toggle Egon's phone 'banking mode' pause.

Why
---
Egon's phone keepalive normally keeps Android Wireless Debugging ON so the
Inbox drain can reach the phone. Banking apps (Nubank and friends) run
anti-fraud checks that REFUSE TO LAUNCH while debugging is on — so while Egon
holds debugging on, those apps won't open.

Egon already auto-detects a banking app in the foreground and backs off, but
this script is the manual override: turn banking mode ON before a long banking
session (or for an app Egon doesn't know about) and Egon will stop re-enabling
Wireless Debugging until you turn it OFF again.

Usage
-----
    python scripts/phone_banking_mode.py on      # pause — let banking apps open
    python scripts/phone_banking_mode.py off     # resume the normal phone link
    python scripts/phone_banking_mode.py toggle
    python scripts/phone_banking_mode.py status

The flag is just the presence of state/panop/phone_link_paused.json, which the
keepalive checks every cycle — no Egon restart needed.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAUSE_FILE = ROOT / "state" / "panop" / "phone_link_paused.json"


def _on() -> None:
    PAUSE_FILE.parent.mkdir(parents=True, exist_ok=True)
    PAUSE_FILE.write_text(json.dumps({
        "paused": True,
        "set_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "note": "Banking mode: Egon will not re-enable Wireless Debugging so "
                "banking apps (Nubank, …) can open. Delete this file or run "
                "`phone_banking_mode.py off` to resume the phone link.",
    }, indent=2), encoding="utf-8")


def _off() -> None:
    try:
        PAUSE_FILE.unlink()
    except FileNotFoundError:
        pass


def main(argv: list[str]) -> int:
    cmd = (argv[0] if argv else "status").lower()
    if cmd in ("on", "pause", "bank"):
        _on(); print("banking mode ON — Egon will let banking apps open")
    elif cmd in ("off", "resume", "unpause"):
        _off(); print("banking mode OFF — normal phone link resumes")
    elif cmd == "toggle":
        if PAUSE_FILE.exists():
            _off(); print("banking mode OFF — normal phone link resumes")
        else:
            _on(); print("banking mode ON — Egon will let banking apps open")
    elif cmd == "status":
        print("banking mode is", "ON" if PAUSE_FILE.exists() else "OFF")
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
