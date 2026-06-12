# Files Integration — the big play (groundwork laid 2026-06-12)

Bruno's goal: Drive + PC files become first-class citizens of the flow — the
Connection Engine, the mind, and eventually the Notion/Obsidian mirror see
every file as an entity, connected by semantic similarity.

## What the survey found (shapes everything)

| Root | Contents | Caveat |
|---|---|---|
| `C:\Users\bruno\Google Drive` | **43,476 PDFs / 57 GB** — the paper+book library | Drive File Stream **cloud placeholders**: metadata is free, reading content force-downloads |
| `C:\Users\bruno\Documents` | ~30k files, mostly code trees | only ~800 knowledge-bearing files after extension curation |
| `My Drive`, `EgonVault` | placeholder roots, ~empty locally | populated on demand by Drive |

Key consequence: **naive full-text indexing would download 57 GB.** The
design is tiered to never do that by accident.

## Tier 1 — SHIPPED (this commit)

- `lib/file_indexer.py` crawls the roots and writes `state/files_index.jsonl`
  (path/name/ext/size/mtime; metadata only, placeholders never hydrated).
  First build: 44,281 files in 107 s.
- `lib/semantic_index.py` gained a `files` source: embeds filename + parent
  folders. Academic filenames are semantically rich, so the whole library
  already surfaces in Connect/bubble/widget results, with `file:///` links.
- Refresh rides the egon_core `connect_index` unit (every 6 h).
- Sweep visibility via `lib/adapters/local_files.py`.

## Tier 2 — budgeted content extraction (next)

- For PDFs that are ALREADY hydrated locally (Drive marks them; detectable
  via `os.stat` block allocation) extract first ~10 pages with pypdf and
  re-embed the same uid with fuller text.
- Daily byte budget (e.g. 200 MB) for deliberately hydrating high-value
  files: most-recently-opened, most-connected, or explicitly pinned.
- Mouseion/PaperGuru already OCR/parse papers — reuse, don't duplicate.

## Tier 3 — entity instantiation (the mirror)

- Each file uid becomes an entity in the Notion/Obsidian mirror (the same
  way Zotero refs and bookmarks do), with semantic-neighbor links computed
  from the Connect index.
- Dedup pass: a Drive PDF, its Zotero attachment, and its Paperpile entry
  are ONE entity with three locations.

## Decisions to make before tier 2

1. Hydration budget size and trigger (auto vs pinned-only).
2. Whether Documents code trees deserve a separate "code" index (different
   embedding model, different surfaces) or stay excluded.
3. Where extracted text lives: alongside the index (jsonl) vs mind.db blobs
   (bears on the day-to-day/long-term DB split — see archival-tier memory).
