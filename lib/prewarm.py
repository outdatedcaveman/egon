"""Pre-warm caches at startup so first page render is instant.

Runs in a background thread when Egon boots. Touches every adapter's live_status()
and Panop's in-process status so when the user clicks ANY tab, all the slow I/O
has already completed and the cache is hot.
"""
from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger("egon.prewarm")

_DONE = False
_LOCK = threading.Lock()


def _do_prewarm():
    global _DONE
    log.info("pre-warm: starting")
    t0 = time.time()

    # Panop module + status
    try:
        from lib import panop_client
        panop_client.is_up()
        panop_client.status()
        panop_client.history_meta()
        log.info("pre-warm: panop OK")
    except Exception as e:
        log.warning("pre-warm: panop failed %s", e)

    # All adapter live_status calls — populates the 60s status cache
    from lib.status_cache import get_status
    sources = [
        ("instapaper",       "lib.adapters.instapaper"),
        ("letterboxd",       "lib.adapters.letterboxd"),
        ("chrome_bookmarks", "lib.adapters.chrome_bookmarks"),
        ("zotero",           "lib.adapters.zotero_local"),
        ("zotero_web",       "lib.adapters.zotero_web"),
        ("notion",           "lib.adapters.notion"),
        ("notion_workspace", "lib.adapters.notion_workspace"),
        ("gdrive",           "lib.adapters.gdrive"),
        ("youtube_music",    "lib.adapters.youtube"),
        ("gcalendar",        "lib.adapters.gcalendar"),
        ("gmail",            "lib.adapters.gmail"),
        ("gfit",             "lib.adapters.gfit"),
        ("paperpile",        "lib.adapters.paperpile"),
        ("tvtime",           "lib.adapters.tvtime"),
        ("mouseion",         "lib.adapters.mouseion"),
    ]
    threads = []
    for sid, mod in sources:
        def probe(sid=sid, mod=mod):
            try:
                get_status(sid, mod_path=mod)
            except Exception as e:
                log.warning("pre-warm %s failed: %s", sid, e)
        t = threading.Thread(target=probe, daemon=True, name=f"prewarm-{sid}")
        t.start()
        threads.append(t)
    # wait at most 10s for parallel probes
    for t in threads:
        t.join(timeout=10)

    with _LOCK:
        _DONE = True
    log.info("pre-warm: done in %.1fs", time.time() - t0)


def start_async():
    """Fire-and-forget background pre-warm. Safe to call multiple times — no-op if already done."""
    with _LOCK:
        if _DONE:
            return
    threading.Thread(target=_do_prewarm, daemon=True, name="egon-prewarm").start()


def is_done() -> bool:
    return _DONE
