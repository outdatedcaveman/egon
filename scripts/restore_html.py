"""Generate a fallback HTML page listing all closed URLs as tappable links.

Use case: when the WebSocket-based restore is unreliable (Chrome on Android
loses its DevTools socket every time the phone is actively used), this HTML
gives Bruno a recoverable list he can open in any browser and walk through
at his own pace.

Output: state/restore/2026-05-15_restore.html
"""
from __future__ import annotations

import html
import json
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "state" / "restore" / "2026-05-15_filtered_to_restore.json"
OUT = ROOT / "state" / "restore" / "2026-05-15_restore.html"


def main() -> int:
    items = json.loads(MANIFEST.read_text(encoding="utf-8"))
    # Group by host so Bruno can scan section by section
    by_host: dict[str, list[dict]] = defaultdict(list)
    for it in items:
        try: host = (urlparse(it.get("closed_url", "")).hostname or "?").lower().replace("www.", "", 1)
        except Exception: host = "?"
        by_host[host].append(it)

    hosts_sorted = sorted(by_host.items(), key=lambda x: -len(x[1]))

    head = ('<!doctype html><html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>Restore tabs 2026-05-15</title>'
            '<style>'
            'body{font:14px -apple-system,sans-serif;max-width:760px;margin:0 auto;padding:16px;color:#222}'
            'h1{font-size:18px}'
            'h2{font-size:14px;color:#444;margin-top:24px;border-top:1px solid #ddd;padding-top:8px}'
            '.row{padding:8px 0;border-bottom:1px dotted #eee}'
            '.row a{color:#1a6dad;word-break:break-all;text-decoration:none}'
            '.row a:visited{color:#888}'
            '.meta{font-size:11px;color:#888;margin-top:2px}'
            '.ai{color:#b15}.redirect{color:#06a}.domain{color:#696}'
            '.title{font-weight:600;color:#333}'
            '.toc{margin:16px 0}'
            '.toc a{display:inline-block;margin:0 8px 4px 0;font-size:12px;color:#1a6dad}'
            '</style></head><body>'
            '<h1>Restore tabs from 2026-05-15 incident</h1>'
            f'<p>Tap each link to re-open. Visited links go grey, so you can track progress.<br>'
            f'<b>{len(items)} URLs total</b>, grouped by host (largest first).</p>'
            '<div class="toc">')
    parts = [head]

    for host, rows in hosts_sorted:
        parts.append(f'<a href="#h-{html.escape(host)}">{html.escape(host)} ({len(rows)})</a>')
    parts.append('</div>')

    for host, rows in hosts_sorted:
        parts.append(f'<h2 id="h-{html.escape(host)}">{html.escape(host)} <span style="color:#aaa;font-weight:normal">({len(rows)})</span></h2>')
        for r in rows:
            cls = r.get("classified_by", "?")
            tag = {"ai_fallback": "ai", "scinews_redirect": "redirect", "domain_rule": "domain"}.get(cls, "")
            title = (r.get("title") or "").strip()[:160]
            url = r.get("closed_url") or ""
            parts.append(f'''<div class="row">
              {f'<div class="title">{html.escape(title)}</div>' if title else ''}
              <a href="{html.escape(url)}" rel="noopener noreferrer">{html.escape(url)}</a>
              <div class="meta">classified by <span class="{tag}">{html.escape(cls)}</span>{(" → " + html.escape(r.get("classified_as") or "")) if r.get("classified_as") else ""}</div>
            </div>''')

    parts.append('</body></html>')
    OUT.write_text("\n".join(parts), encoding="utf-8")
    print(f"wrote {OUT}")
    print(f"  size: {OUT.stat().st_size} bytes")
    print(f"  urls: {len(items)}")
    print(f"  hosts: {len(by_host)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
