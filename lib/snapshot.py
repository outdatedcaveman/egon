"""Live adapter snapshot writer.

Probes every adapter under `lib.adapters` that exposes a `live_status()`
function, aggregates the results, and writes a complete `last_pass.json`
with `generated_at`, `sources`, and basic items_processed/duration metrics.

Used by:
  • The "Run pass now" button in the native app
  • CLI:  python -m lib.snapshot
  • Scheduled tasks (KMS-Egon-Snapshot every 30 min)

Per-adapter probes run in parallel with a hard per-adapter timeout, so a
single dead source can't stall the whole snapshot.
"""
from __future__ import annotations

import importlib
import json
import os
import pkgutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Where to write. The local file keeps Egon useful even when Drive is slow,
# offline, or blocked by sandboxed tooling. The vault copy is the off-machine
# mirror used by the wider KMS.
LOCAL_STATE = Path(__file__).resolve().parent.parent / "state"
LOCAL_LAST_PASS = LOCAL_STATE / "last_pass.json"
from lib.egon_paths import VAULT_STATE
LAST_PASS = VAULT_STATE / "last_pass.json"
LAST_PASS_TARGETS = (LOCAL_LAST_PASS, LAST_PASS)

# Adapters to skip — either non-source helpers or known-broken.
_SKIP = {"_stubs", "base", "panop_capture", "phone_discovery"}

# Hard timeout per adapter — defends against network hangs. Raised from 8 s
# to 45 s on 2026-05-26 because (a) Drive-backed probes (vault) need 10+ s on
# a cold Drive mount, and (b) Zotero web and large Paperpile BibTeX parsing
# can exceed 20s. Single adapter still can't stall the rest because
# we run them concurrently.
_PER_ADAPTER_TIMEOUT_S = 45.0


def _list_adapters() -> list[str]:
    pkg = importlib.import_module("lib.adapters")
    out: list[str] = []
    for _finder, name, ispkg in pkgutil.iter_modules(pkg.__path__):
        if ispkg or name.startswith("_") or name in _SKIP:
            continue
        out.append(name)
    return sorted(out)


def _probe_one(adapter_id: str) -> tuple[str, dict[str, Any]]:
    """Call live_status() on one adapter with a hard timeout."""
    start = time.time()
    result: dict[str, Any] = {"status": "error", "error": "not probed"}

    def _run() -> None:
        nonlocal result
        try:
            mod = importlib.import_module(f"lib.adapters.{adapter_id}")
            if hasattr(mod, "live_status"):
                r = mod.live_status()
                if isinstance(r, dict):
                    result = r
                else:
                    result = {"status": "error", "error": "live_status didn't return dict"}
            else:
                result = {"status": "skip", "error": "no live_status function"}
        except Exception as e:
            result = {"status": "error", "error": f"{type(e).__name__}: {e}"[:200]}

    t = threading.Thread(target=_run, daemon=True, name=f"probe-{adapter_id}")
    t.start()
    t.join(timeout=_PER_ADAPTER_TIMEOUT_S)
    if t.is_alive():
        result = {"status": "timeout",
                  "error": f"probe exceeded {_PER_ADAPTER_TIMEOUT_S}s"}
    result["_probe_ms"] = int((time.time() - start) * 1000)
    return adapter_id, result


def _read_existing_last_pass() -> dict[str, Any]:
    """Read the newest existing last_pass file from local or vault storage."""
    candidates = [p for p in LAST_PASS_TARGETS if p.exists() and p.stat().st_size > 0]
    if not candidates:
        return {}
    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    try:
        return json.loads(newest.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def snapshot(write: bool = True, max_workers: int = 12) -> dict[str, Any]:
    """Walk every adapter, build a sources block, write last_pass.json.
    Returns the snapshot dict whether or not it was written."""
    started = time.time()
    adapters = _list_adapters()
    sources: dict[str, dict] = {}

    # Deliberately-disabled adapters (egon-config.json adapters.disabled:
    # {name: reason}). These report "off" — a CHOICE, not a failure — so the
    # UI stops counting redundant/heavy optional tools (letta, mem0,
    # openrefine, anystyle) as work waiting to happen. Bruno 2026-06-12.
    disabled: dict = {}
    try:
        cfg = json.loads((Path(__file__).resolve().parent.parent
                          / "egon-config.json").read_text(encoding="utf-8"))
        disabled = (cfg.get("adapters") or {}).get("disabled") or {}
    except Exception:
        pass
    for name, reason in disabled.items():
        if name in adapters:
            adapters.remove(name)
            sources[name] = {"status": "off", "note": str(reason)}

    # WARM the import graph sequentially before any threaded probing — Python
    # import locks aren't safe under concurrent first-imports of modules whose
    # transitive deps overlap. We hit this with `instapaper_full` (whose
    # `requests_oauthlib` raced `requests.utils`). Touching every module once
    # in the main thread costs ~200 ms and removes the entire class of error.
    for aid in adapters:
        try:
            importlib.import_module(f"lib.adapters.{aid}")
        except Exception:
            pass    # broken adapters still get probed below and report error

    # Preserve any extra fields from the existing file we don't manage here
    # (notably `ledger`, which is computed by the agent's full pass).
    existing = _read_existing_last_pass()

    with ThreadPoolExecutor(max_workers=max_workers,
                            thread_name_prefix="snapshot") as pool:
        futs = {pool.submit(_probe_one, a): a for a in adapters}
        total_timeout = _PER_ADAPTER_TIMEOUT_S * (
            max(1, (len(adapters) + max_workers - 1) // max_workers)
        ) + 5
        completed = set()
        try:
            for f in as_completed(futs, timeout=total_timeout):
                completed.add(f)
                try:
                    aid, res = f.result()
                    sources[aid] = res
                except Exception as e:
                    aid = futs.get(f, f"_unknown_{id(f)}")
                    sources[aid] = {"status": "error", "error": str(e)[:200]}
        except TimeoutError:
            pass

        for f, aid in futs.items():
            if f not in completed and aid not in sources:
                sources[aid] = {"status": "timeout",
                                "error": f"snapshot exceeded {round(total_timeout, 1)}s"}

    # Merge new probe results into existing sources to preserve agent-computed metrics
    merged_sources = {}
    existing_sources = existing.get("sources") or {}
    for aid, res in sources.items():
        if aid in existing_sources and isinstance(existing_sources[aid], dict):
            m_res = dict(existing_sources[aid])
            m_res.update(res)
            merged_sources[aid] = m_res
        else:
            merged_sources[aid] = res
    for aid, res in existing_sources.items():
        if aid not in merged_sources:
            merged_sources[aid] = res

    # Build the output starting from existing to preserve all agent-computed top-level keys
    out: dict[str, Any] = dict(existing)
    duration = time.time() - started
    out.update({
        "schema_version": "0.3.0",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generator": "lib.snapshot",
        "duration_seconds": round(duration, 2),
        "items_processed": sum(
            v.get("count") or v.get("queue_count") or 0
            for v in merged_sources.values() if isinstance(v, dict)
        ),
        "sources": merged_sources,
    })
    if write:
        written: list[str] = []
        warnings: list[str] = []
        for target in LAST_PASS_TARGETS:
            try:
                _atomic_write_json(target, out)
                written.append(str(target))
            except Exception as e:
                warnings.append(f"{target}: {e}"[:260])
        out["_written"] = written
        if warnings:
            out["_write_warning"] = warnings
        if not written:
            out["_write_error"] = "no last_pass target could be written"
    return out


def main() -> int:
    # cmd.exe defaults to cp1252; force UTF-8 stdout so emoji/arrows don't crash
    import sys as _sys, io as _io
    try:
        _sys.stdout = _io.TextIOWrapper(_sys.stdout.buffer, encoding="utf-8")
    except Exception:
        pass
    print(f"[snapshot] probing adapters -> {LOCAL_LAST_PASS} + {LAST_PASS}")
    r = snapshot(write=True)
    n_ok = sum(1 for v in r["sources"].values()
               if str(v.get("status", "")).lower() in ("ok", "alive"))
    n_total = len(r["sources"])
    print(f"[snapshot] done in {r['duration_seconds']}s — "
          f"{n_ok}/{n_total} adapters OK")
    for aid, v in sorted(r["sources"].items()):
        st = v.get("status", "?")
        extra = ""
        for k in ("count", "queue_count", "items", "last_seen"):
            if k in v:
                extra = f"  {k}={v[k]}"
                break
        if "error" in v:
            extra += f"  err={v['error'][:60]}"
        print(f"  {aid:18s}  {st:8s} {extra}")
    if r.get("_write_warning"):
        print("[snapshot] WRITE WARNING:")
        for warning in r["_write_warning"]:
            print(f"  {warning}")
    if r.get("_write_error"):
        print(f"[snapshot] WRITE FAILED: {r['_write_error']}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
