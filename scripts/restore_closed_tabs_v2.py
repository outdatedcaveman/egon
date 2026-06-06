"""Resilient tab-restoration v2.

Improvements over v1:
  - Slower rate (500ms between sends) to avoid Chrome OOM-killing DevTools.
  - Fire-and-forget WebSocket sends — don't await per-request response (Chrome
    sends async Target.targetCreated events that confused v1's response matcher).
  - Checkpoint file (state/restore/checkpoint.json) tracks which manifest
    indices have been pushed; resumes from there on restart.
  - WebSocket dies → reconnect loop with backoff; keeps going.
  - Health-check via REST /json/list every 50 URLs; if tab count stops growing
    for 2 consecutive checks, pause + reconnect.
  - Skips URLs already present in the live tab list (don't re-open the same
    tab twice across restarts).
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
import websockets

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "state" / "restore" / "2026-05-15_filtered_to_restore.json"
CHECKPOINT = ROOT / "state" / "restore" / "checkpoint.json"
LOG_FILE = ROOT / "logs" / "restore-v2-2026-05-15.log"

SLEEP_PER_URL = 0.5     # 500ms between sends
HEALTH_CHECK_EVERY = 50
DEVTOOLS = "http://127.0.0.1:9222"
SAVE_CHECKPOINT_EVERY = 10


def _log(level: str, **kw):
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    entry = {"ts": datetime.now().isoformat(timespec="seconds"), "level": level, **kw}
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _load_checkpoint() -> dict:
    if CHECKPOINT.exists():
        try: return json.loads(CHECKPOINT.read_text(encoding="utf-8"))
        except Exception: return {}
    return {}


def _save_checkpoint(state: dict) -> None:
    CHECKPOINT.parent.mkdir(parents=True, exist_ok=True)
    CHECKPOINT.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _current_tab_count() -> int:
    try:
        r = requests.get(f"{DEVTOOLS}/json/list", timeout=8)
        return len(r.json()) if r.status_code == 200 else -1
    except Exception:
        return -1


def _current_tab_urls() -> set:
    try:
        r = requests.get(f"{DEVTOOLS}/json/list", timeout=10)
        return {t.get("url") for t in r.json() if t.get("url")}
    except Exception:
        return set()


async def _connect_ws():
    """Establish a fresh WebSocket connection to the browser endpoint."""
    info = requests.get(f"{DEVTOOLS}/json/version", timeout=10).json()
    ws_url = info["webSocketDebuggerUrl"]
    return await websockets.connect(ws_url, max_size=10 * 1024 * 1024, ping_interval=20)


async def restore_all():
    items = json.loads(MANIFEST.read_text(encoding="utf-8"))
    total = len(items)
    state = _load_checkpoint()
    start_idx = state.get("next_idx", 0)
    already_open = _current_tab_urls()
    initial_count = _current_tab_count()

    print(f"Total in manifest: {total}")
    print(f"Resuming from idx: {start_idx}")
    print(f"Current tab count on phone: {initial_count}")
    print(f"Already-open URLs (will skip): {len(already_open)}")
    _log("info", event="start", total=total, resume_from=start_idx,
         initial_count=initial_count, already_open=len(already_open))

    ws = None
    last_health_count = initial_count
    health_no_growth_consec = 0
    next_id = 1
    pushed = 0
    skipped_already_open = 0

    async def _reconnect_with_backoff():
        nonlocal ws
        if ws is not None:
            try: await ws.close()
            except Exception: pass
        for attempt in range(6):
            try:
                ws = await _connect_ws()
                _log("info", event="ws_reconnected", attempt=attempt)
                return True
            except Exception as e:
                _log("warn", event="ws_reconnect_fail", attempt=attempt, error=str(e)[:200])
                await asyncio.sleep(5 + attempt * 5)
        return False

    if not await _reconnect_with_backoff():
        _log("error", event="initial_ws_failed")
        print("Could not connect to Chrome DevTools. Phone unreachable?")
        return 1

    try:
        for i in range(start_idx, total):
            item = items[i]
            url = item.get("closed_url") or ""
            if not url or not url.startswith(("http://", "https://")):
                state["next_idx"] = i + 1
                continue
            if url in already_open:
                skipped_already_open += 1
                state["next_idx"] = i + 1
                if (i + 1) % SAVE_CHECKPOINT_EVERY == 0: _save_checkpoint(state)
                continue

            # Fire-and-forget send
            req_id = next_id; next_id += 1
            payload = json.dumps({
                "id": req_id, "method": "Target.createTarget",
                "params": {"url": url, "background": True},
            })
            try:
                await ws.send(payload)
                pushed += 1
            except Exception as e:
                _log("warn", event="send_failed", idx=i, error=str(e)[:150])
                if not await _reconnect_with_backoff():
                    _log("error", event="reconnect_gave_up", idx=i)
                    print("WS reconnection failed permanently. Saving checkpoint.")
                    break
                # Retry once after reconnect
                try:
                    await ws.send(payload); pushed += 1
                except Exception:
                    pass

            state["next_idx"] = i + 1
            if (i + 1) % SAVE_CHECKPOINT_EVERY == 0: _save_checkpoint(state)

            # Drain any pending response messages (don't accumulate buffer)
            try:
                while True:
                    msg = await asyncio.wait_for(ws.recv(), timeout=0.01)
                    # ignore content; just keep recv buffer clear
                    del msg
            except asyncio.TimeoutError:
                pass
            except Exception as e:
                _log("warn", event="recv_drain_error", error=str(e)[:150])
                if not await _reconnect_with_backoff():
                    break

            # Health-check
            if (i + 1) % HEALTH_CHECK_EVERY == 0:
                cnt = _current_tab_count()
                pct = (i + 1) / total * 100
                print(f"  [{i+1}/{total}] {pct:.1f}%  pushed={pushed}  skipped={skipped_already_open}  tabs_on_phone={cnt}")
                _log("info", event="health", done=i+1, pushed=pushed,
                     skipped=skipped_already_open, tab_count=cnt)
                if cnt > 0 and cnt <= last_health_count:
                    health_no_growth_consec += 1
                    if health_no_growth_consec >= 2:
                        _log("warn", event="no_growth", tab_count=cnt, last=last_health_count)
                        if not await _reconnect_with_backoff():
                            break
                        health_no_growth_consec = 0
                else:
                    health_no_growth_consec = 0
                last_health_count = cnt
                # Refresh the already-open set for future skipping
                already_open = _current_tab_urls()

            await asyncio.sleep(SLEEP_PER_URL)
    finally:
        _save_checkpoint(state)
        if ws is not None:
            try: await ws.close()
            except Exception: pass

    final = _current_tab_count()
    _log("info", event="done", pushed=pushed, skipped=skipped_already_open,
         next_idx=state.get("next_idx", 0), final_tab_count=final)
    print(f"\nDONE. pushed={pushed} skipped_already_open={skipped_already_open}")
    print(f"  checkpoint saved at idx {state.get('next_idx',0)} of {total}")
    print(f"  final tab count on phone: {final} (started at {initial_count})")
    return 0


def main():
    if not MANIFEST.exists():
        print(f"manifest not found: {MANIFEST}"); return 1
    return asyncio.run(restore_all())


if __name__ == "__main__":
    raise SystemExit(main())
