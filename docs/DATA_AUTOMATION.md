# Data automation — once-for-all, no manual export loops

Bruno's standing rule (2026-06-13): **"I don't want to keep exporting data
and uploading regularly for ANY service. We need once-for-all solutions."**

Every source must be hands-off after at most ONE setup. Here's the status
and the automation mechanism for each.

## Fully automatic (zero ongoing action)

| Source | Mechanism | Cadence |
|---|---|---|
| Zotero (252k) | local SQLite read | daily snapshot |
| Chrome bookmarks/tabs | extension harvest | 60 min |
| YouTube likes/playlists/subs | OAuth API + refresh token | daily |
| Trakt (TV/film) | OAuth API + refresh token | daily *(once authed)* |
| Paperpile, Instapaper, Pocket Casts, Letterboxd, Kindle, TV Time | extension harvest (merge-accumulate) | 60 min / on visit |
| Notion workspace + page bodies | Notion API | 5 min |
| Mind (all 3 agents) | pull ingest + hooks + MCP | 60 s |
| PC + Drive files | file indexer | 6 h |

## One-time setup, then automatic

### Google data (Takeout: YouTube history, Fit, Health, Gemini, My Activity)
The complete history only exists in Takeout — BUT Takeout exports can be
**scheduled and auto-delivered to Google Drive**, which the Drive desktop
mount syncs locally, which `lib/export_inbox` auto-scans. So:

  **Setup once:** takeout.google.com → select the products → "Frequency:
  **Export every 2 months for 1 year**" → "Destination: **Add to Drive**".

  After that: every 2 months a fresh export lands in Drive/Takeout →
  `export_inbox.process()` (daily snapshots unit) detects + parses it
  automatically. **Zero uploads, zero clicks, forever.** Re-confirm the
  schedule once a year (Google caps at 1 year).

`export_inbox` auto-watches: `state/inbox/`, `Google Drive/Takeout`,
`My Drive/Takeout`, and `Downloads` (export-looking zips only). Dropping a
zip anywhere in those is enough; the scheduled-to-Drive path needs no drop.

### Amazon / Kindle (Documents + full library)
Amazon DSAR ("Request My Data") is one-time per request, not scheduled — but
the **extension harvest now merge-accumulates** (never overwrites), so the
ongoing Documents/PDOC updates flow hands-off. The DSAR is only needed once
as a complete backfill; drop its zip in Downloads and it's absorbed.

### Trakt (replaces TV Time)
One OAuth authorize (device code at trakt.tv/activate) → refresh token →
permanent. Trakt auto-scrobbles from streaming services, so going forward
new watches flow with zero action. Seed once from the TV Time harvest via
`trakt.push_tvtime_history()`.

## The principle in code
- `lib/export_inbox._WATCH_DIRS` — the folders auto-scanned each cycle.
- `_looks_like_export()` — only export-shaped zips are touched outside inbox.
- Idempotent by name+mtime, so re-scans are free and safe.
- Everything funnels through the daily `snapshots` unit in `egon_core`.
