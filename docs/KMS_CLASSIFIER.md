# KMS master classifier — one brain for all link-classifying surfaces

Bruno 2026-06-15. Every surface that classifies a web link — egon **Inbox** /
standalone **Panop**, egon **Navigation** / standalone **Routster**, and any
future process — must use ONE engine so a link is never categorised two ways.
This documents that engine.

## The engine (`lib/classifier`)

`lib.classifier.classify(url, page_meta) -> ClassificationResult`. Layered, in
order; first confident layer wins:

1. **domain_tiers** — authoritative `always_academic_*` hosts (arxiv→articles,
   nature→articles, phys.org→science_news…). Fast, exact. (The old
   `never_academic` short-circuit was **removed** 2026-06-15 — it blocked the ML
   from classifying github/wikipedia/medium, which are real categories now.)
2. **hard_gates** — citation meta tags + paper-only URL patterns
   (`/doi/`, `/abs/`, `/pdf/`, arxiv/preprint). `/articles/` and `/article/`
   were **removed** — every news site uses them (BBC `/sport/.../articles/`).
3. **kms_knn** (`lib/kms_knn.py`) — the native ML, **default brain**. Weighted
   k-NN over MiniLM title embeddings, trained on Bruno's own bookmark folders.
   Confidence ≥0.50 → match; 0.35–0.50 → review; <0.35 → abstain (dubious).
4. **embeddings** — legacy centroid fallback.

Exposed to non-Python surfaces as **`POST /api/v1/classify`** on the Panop
server (`{url,title,abstract?,fetch?}` → `{category,confidence,layer}`). Routster
(JS) and any new tool call this instead of running their own classifier.

## Two-tier design (Bruno's rule)

- **ML decides the confident bulk** — token-free, runs in every app by default,
  no AI prompting needed.
- **The powerful AI (a Claude Code session) is the arbiter for the dubious tail**
  only (kNN confidence <0.50). Its verdicts + Bruno's manual category overrides
  are appended to the training set (`kms_knn.learn()`); a rebuild re-indexes, so
  the ML converges toward the AI over time and the AI is needed less and less.

## Taxonomy + destinations

Trained from Bruno's `KMS Output` + top-level bookmark folders (≈150k curated
links → `scripts/build_kms_training.py` → 14k balanced exemplars):

| category | destination |
|---|---|
| articles | Zotero Panop/Articles + bookmark |
| books | Zotero Panop/Books + bookmark |
| science_news | Zotero Panop/Science News + bookmark (aggregators → 2nd-stage) |
| content_longform | Instapaper + bookmark (read-later: substack/medium/aeon…) |
| references | bookmark only (Wikipedia/SEP/nLab/Britannica) |
| data_tools | bookmark only (github/huggingface/tools) |
| shopping | bookmark only (products to buy, NOT books) |
| opportunities | bookmark only (jobs/fellowships/grants) |
| study_work | bookmark only (courses/assets/career) |
| curios | bookmark only |
| reject | nothing |

Same Amazon domain → **books** (a book) or **shopping** (cutlery/camera) by
TITLE — that's why the ML trains on titles, not domains. The 7 non-Zotero
categories are wired into `panop_config.json` with `route:"bookmark"`;
`send_to_zotero` skips them.

## Re-training / maintenance

- `scripts/build_kms_training.py` → rebuild the labelled set from bookmarks.
- `python -c "import lib.kms_knn as k; k.build_index()"` → rebuild the kNN index.
- New AI/manual labels: `kms_knn.learn(title,url,category)` then rebuild.

## What ran (2026-06-15)

Full Chrome history (Takeout, 14,405 unique URLs) classified by this engine —
ML for the confident bulk + AI arbiter for 2,937 dubious (paced 600-link
workflows, never cut). 12,203 saveable → 5,952 new Zotero, 1,281 Instapaper,
10,103 bookmarks in Panop/<category> folders. All reversible + traced
(`state/panop/*_ledger.jsonl`, backups in `state/panop/backups/`).

## Pending

- Point standalone Panop (`~/Desktop/Panop/panop-server`) and standalone
  Routster (`~/Documents/Workspace/kms_auto_router`, Node) at `/api/v1/classify`
  and restart standalone Panop — so all four surfaces share this brain.
- content_longform live-drain routing currently bookmarks (Instapaper routing in
  the drain is a follow-up; the history backfill already sent it to Instapaper).

See [[feedback_panop_routster_mirror]], [[feedback_data_safety_and_classifier]].
