# Files Integration — the big play (groundwork laid 2026-06-12)

Bruno's goal: Drive + PC files become first-class citizens of the flow — the
Connection Engine, the mind, and eventually the Notion/Obsidian mirror see
every file as an entity, connected by semantic similarity.

## What the survey found (shapes everything)

| Root | Contents | Caveat |
|---|---|---|
| `~/Google Drive` | **43,476 PDFs / 57 GB** — the paper+book library | Drive File Stream **cloud placeholders**: metadata is free, reading content force-downloads |
| `~/Documents` | ~30k files, mostly code trees | only ~800 knowledge-bearing files after extension curation |
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

## Tier 2 - SHIPPED (budgeted content extraction)

- Pinned files still go through `lib/hydration_worker.py`, with a per-run byte
  cap before content is opened.
- `lib/auto_hydrate_crawler.py` now scans the indexed corpus for supported
  local formats (`pdf`, text/markup/data files, `docx`, `pptx`, `epub`, `odt`)
  and writes extracts to `state/file_extracts/` before the semantic rebuild.
- Cloud-backed Drive files are only opened when Windows reports them locally
  available, so placeholder files are not force-downloaded by the crawler.
- `lib/semantic_index.py` chunks extracted file text and embeds each chunk with
  filename/path context, while files without extracts keep metadata vectors.
- Mouseion/PaperGuru already OCR/parse papers - reuse, don't duplicate.

## Tier 3 — entity instantiation (the mirror)

- Each file uid becomes an entity in the Notion/Obsidian mirror (the same
  way Zotero refs and bookmarks do), with semantic-neighbor links computed
  from the Connect index.
- Dedup pass: a Drive PDF, its Zotero attachment, and its Paperpile entry
  are ONE entity with three locations.

## Decisions to make after tier 2

1. Whether Documents code trees deserve a separate "code" index (different
   embedding model, different surfaces) or stay excluded.
2. Whether scanned PDFs should get OCR, and which existing OCR/PaperGuru output
   can be reused before adding another extractor.
3. Where extracted text ultimately lives: alongside the index (`file_extracts`)
   vs mind.db blobs (bears on the day-to-day/long-term DB split).
