"""Restoration: re-open closed tabs on phone via Chrome DevTools Protocol (WebSocket).

The REST endpoint `PUT /json/new?<url>` was removed in modern Android Chrome
(returns HTTP 500). The WebSocket-based `Target.createTarget` method still
works and is what we use here.

Reads `state/restore/2026-05-15_filtered_to_restore.json` and opens each URL
as a new tab on the phone via a single persistent WebSocket connection.
Logs every push (and any failures) to logs/restore-2026-05-15.log.
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

import requests
import websockets

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "state" / "restore" / "2026-05-15_filtered_to_restore.json"
LOG_FILE = ROOT / "logs" / "restore-2026-05-15.log"

SLEEP_PER_URL = 0.08       # 80ms between Target.createTarget calls
BATCH_PAUSE_EVERY = 50     # extra pause every N URLs to let Chrome catch up
BATCH_PAUSE_S = 2.0
DEVTOOLS = "http://127.0.0.1:9222"


def _log(level: str, **kw):
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    entry = {"ts": datetime.now().isoformat(timespec="seconds"), "level": level, **kw}
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


async def restore_all(items):
    info = requests.get(f"{DEVTOOLS}/json/version", timeout=10).json()
    ws_url = info["webSocketDebuggerUrl"]
    _log("info", event="ws_connect", ws_url=ws_url, browser=info.get("Browser"))

    ok = 0
    fail = 0
    next_id = 1
    async with websockets.connect(ws_url, max_size=10 * 1024 * 1024, ping_interval=20) as ws:
        for i, item in enumerate(items):
            url = item.get("closed_url") or ""
            if not url or not url.startswith(("http://", "https://")):
                fail += 1
                _log("warn", event="skipped_invalid_url", idx=i, url=url[:80])
                continue
            try:
                req_id = next_id; next_id += 1
                await ws.send(json.dumps({
                    "id": req_id,
                    "method": "Target.createTarget",
                    "params": {"url": url, "background": True},
                }))
                resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                if resp.get("id") == req_id and "result" in resp and resp["result"].get("targetId"):
                    ok += 1
                else:
                    fail += 1
                    _log("warn", event="push_no_targetid", idx=i, url=url[:120],
                         response=str(resp)[:200])
            except asyncio.TimeoutError:
                fail += 1
                _log("warn", event="push_timeout", idx=i, url=url[:120])
            except Exception as e:
                fail += 1
                _log("warn", event="push_exception", idx=i, url=url[:120], error=str(e)[:150])

            if (i + 1) % 50 == 0:
                pct = (i + 1) / len(items) * 100
                print(f"  [{i+1}/{len(items)}] {pct:.1f}%  ok={ok}  fail={fail}")
                _log("info", event="progress", done=i+1, ok=ok, fail=fail)

            await asyncio.sleep(SLEEP_PER_URL)
            if (i + 1) % BATCH_PAUSE_EVERY == 0:
                await asyncio.sleep(BATCH_PAUSE_S)

    return ok, fail


def main() -> int:
    if not MANIFEST.exists():
        print(f"manifest not found: {MANIFEST}"); return 1
    items = json.loads(MANIFEST.read_text(encoding="utf-8"))
    print(f"restoring {len(items)} URLs via WebSocket DevTools…")
    _log("info", event="restore_start_ws", total=len(items))

    try:
        ok, fail = asyncio.run(restore_all(items))
    except Exception as e:
        _log("error", event="restore_aborted", error=str(e)[:200])
        print(f"ABORTED: {e}"); return 1

    _log("info", event="restore_done", ok=ok, fail=fail, total=len(items))
    print(f"DONE: ok={ok} fail={fail} (of {len(items)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
