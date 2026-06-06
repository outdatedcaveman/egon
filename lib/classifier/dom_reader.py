"""DOM reader — extract page content from the phone's OWN live Chrome tabs.

Why: PC-side scraping (`fetch_page_content`) is blocked by Cloudflare, Amazon's
anti-bot, paywalls, etc. But the phone's Chrome has ALREADY loaded and rendered
every one of those pages — it passed the challenges, it has the real DOM. So we
read the content straight out of the phone's tab via the DevTools
`Runtime.evaluate` method.

This gives us, per tab:
  - title, url, full meta-tag dict
  - a text sample
  - book signals (ISBN, "Publisher", binding type) — incl. Amazon product pages
  - article signals (DOI, "Abstract", citation_* meta)

Requires the DevTools forward to be live on 127.0.0.1:9222 and the `websockets`
library. Works ONLY on tabs that currently have a live renderer (rendered/active
tabs). Frozen tabs have no JS context — for those, fall back to URL-only signals
or wake them first.
"""
from __future__ import annotations

import json

DEVTOOLS = "http://127.0.0.1:9222"

# JS evaluated inside each tab. Returns a JSON-able dict of classification
# signals. Kept defensive — any page, any state.
_EXTRACT_JS = r"""
(() => {
  try {
    const meta = {};
    document.querySelectorAll('meta').forEach(m => {
      const k = (m.getAttribute('name') || m.getAttribute('property') || '').toLowerCase();
      const v = m.getAttribute('content') || '';
      if (k && v) meta[k] = v.slice(0, 500);
    });
    const bodyText = (document.body ? document.body.innerText : '').slice(0, 12000);
    const lower = bodyText.toLowerCase();
    // ISBN: ISBN-10 or ISBN-13, with or without the "ISBN" label
    const isbnRe = /\bisbn(?:-1[03])?\s*:?\s*((?:97[89][\s-]?)?(?:\d[\s-]?){9}[\dxX])\b/i;
    const isbnMatch = bodyText.match(isbnRe);
    // DOI anywhere in text or meta
    const doiRe = /\b10\.\d{4,9}\/[-._;()\/:A-Za-z0-9]+/;
    const doiMatch = bodyText.match(doiRe);
    // Amazon product-detail signals (works for amazon.com and localized sites)
    const amazonBook = (() => {
      if (!location.hostname.includes('amazon.')) return false;
      // Detail bullets / product information table
      const txt = lower;
      const bookHints = ['print length', 'paperback', 'hardcover', 'kindle edition',
        'publisher', 'isbn-13', 'isbn-10', 'idioma', 'capa comum', 'capa dura',
        'editora', 'número de páginas', 'language'];
      const hits = bookHints.filter(h => txt.includes(h)).length;
      // a books breadcrumb is the strongest single signal
      const crumb = document.querySelector('#wayfinding-breadcrumbs_feature_div');
      const crumbBooks = crumb && /books|livros|kindle/i.test(crumb.innerText || '');
      return crumbBooks || hits >= 3;
    })();
    return {
      ok: true,
      title: (document.title || '').slice(0, 400),
      url: location.href,
      meta: meta,
      text_sample: bodyText.slice(0, 4000),
      has_isbn: !!isbnMatch,
      isbn: isbnMatch ? isbnMatch[1].replace(/[\s-]/g, '') : null,
      has_doi: !!doiMatch,
      doi: doiMatch ? doiMatch[0] : null,
      has_abstract: lower.includes('abstract') || ('citation_abstract' in meta) || ('description' in meta),
      amazon_book: amazonBook,
      text_len: bodyText.length,
    };
  } catch (e) {
    return { ok: false, error: String(e).slice(0, 200) };
  }
})()
"""


def read_tabs(target_ids, log_fn=None):
    """Read DOM signals from a batch of live tabs.

    Returns {target_id: signals_dict}. Tabs with no live renderer (frozen) or
    that error are simply absent from the result.
    """
    out = {}
    if not target_ids:
        return out
    try:
        import requests
        from websockets.sync.client import connect as ws_connect
    except Exception as e:
        if log_fn: log_fn("dom_reader_unavailable", error=str(e)[:120])
        return out
    try:
        ver = requests.get(f"{DEVTOOLS}/json/version", timeout=10).json()
        ws_url = ver["webSocketDebuggerUrl"]
    except Exception as e:
        if log_fn: log_fn("dom_reader_no_devtools", error=str(e)[:120])
        return out

    mid = [0]
    def _id():
        mid[0] += 1
        return mid[0]

    try:
        with ws_connect(ws_url, max_size=16 * 1024 * 1024, open_timeout=15) as ws:
            for tid in target_ids:
                try:
                    aid = _id()
                    ws.send(json.dumps({"id": aid, "method": "Target.attachToTarget",
                                        "params": {"targetId": tid, "flatten": True}}))
                    session_id = None
                    for _ in range(25):
                        resp = json.loads(ws.recv(timeout=10))
                        if resp.get("id") == aid:
                            session_id = (resp.get("result") or {}).get("sessionId")
                            break
                        if resp.get("method") == "Target.attachedToTarget":
                            p = resp.get("params") or {}
                            if (p.get("targetInfo") or {}).get("targetId") == tid:
                                session_id = p.get("sessionId")
                    if not session_id:
                        continue
                    eid = _id()
                    ws.send(json.dumps({"id": eid, "sessionId": session_id,
                                        "method": "Runtime.evaluate",
                                        "params": {"expression": _EXTRACT_JS,
                                                   "returnByValue": True,
                                                   "timeout": 8000}}))
                    signals = None
                    for _ in range(25):
                        resp = json.loads(ws.recv(timeout=12))
                        if resp.get("id") == eid:
                            res = (resp.get("result") or {}).get("result") or {}
                            signals = res.get("value")
                            break
                    try:
                        ws.send(json.dumps({"id": _id(),
                                            "method": "Target.detachFromTarget",
                                            "params": {"sessionId": session_id}}))
                    except Exception:
                        pass
                    if isinstance(signals, dict) and signals.get("ok"):
                        out[tid] = signals
                except Exception:
                    continue
    except Exception as e:
        if log_fn: log_fn("dom_reader_session_failed", error=str(e)[:150])
    return out
