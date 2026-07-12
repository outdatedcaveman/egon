"""EgonSearch — dedicated semantic-search worker (127.0.0.1:8801, localhost-only).

Why this process exists (Bruno 2026-07-12, "do it all" / RAM re-architecture):
mind_service ran connection_engine IN-PROCESS, so every search burst stacked the
embedder + turbovec + meta caches on top of FastAPI + orchestrator + ingest —
the measured 789MB baseline spiking to ~1.4GB on the 8GB box (the freeze class).
Isolating the search stack here means:
  • mind_service sheds the whole ~460MB search footprint and stops spiking;
  • a runaway/leaky search burst can be killed/restarted WITHOUT touching the
    always-on mind (egon_core supervises this worker like any unit);
  • the warm model/turbo cache survives mind_service restarts, so phone search
    stays ~1s.
mobile_connect proxies here first and falls back to in-process connect() if
this worker is down (fallback always — nothing breaks if it's absent).

Supervised by egon_core (check_search_worker). Named process per Bruno's
"services with identity" rule. Localhost only; no auth needed beyond that.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Never hit the network resolving models — everything is local (same hints
# mobile_connect used when the stack lived in-process).
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/health")
async def health():
    rss = None
    try:
        import ctypes
        import ctypes.wintypes as wt

        class PMC(ctypes.Structure):
            _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]
        pmc = PMC(); pmc.cb = ctypes.sizeof(PMC)
        h = ctypes.windll.kernel32.GetCurrentProcess()
        if ctypes.windll.psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
            rss = round(pmc.WorkingSetSize / (1024 * 1024))
    except Exception:
        pass
    return {"ok": True, "service": "EgonSearch", "rss_mb": rss}


@app.post("/connect")
async def do_connect(req: Request):
    try:
        body = await req.json()
    except Exception:
        body = {}
    import asyncio
    from lib.connection_engine import connect
    try:
        res = await asyncio.to_thread(
            connect,
            str(body.get("text") or ""),
            int(body.get("limit") or 18),
            bool(body.get("semantic_search", True)),
            bool(body.get("lexical_search", False)),
        )
        return res
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)[:200]},
                            status_code=500)


def main() -> None:
    # Warm the stack off-thread so the first phone query isn't cold.
    import threading

    def _warm():
        try:
            from lib.connection_engine import connect
            connect("warmup", limit=1)
        except Exception:
            pass
    threading.Thread(target=_warm, name="search-warmup", daemon=True).start()

    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8801, log_level="warning",
                access_log=False)


if __name__ == "__main__":
    main()
