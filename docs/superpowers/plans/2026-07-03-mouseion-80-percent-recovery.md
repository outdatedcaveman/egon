# Mouseion 80 Percent Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move Mouseion to at least 80% of entries with PDFs and at least 80% complete entries, where complete means title, authors, publisher, year, and url or doi are present.

**Architecture:** Treat this as a two-lane recovery: metadata completion and PDF acquisition. The first lane rebuilds a DB-safe work queue for records that still fail Bruno's completion predicate; the second lane runs bounded, resumable PDF fetching against the easiest legal/open candidates first, with daily audit evidence written back to Egon.

**Tech Stack:** Python, SQLite, Mouseion `src/mouseion`, Flask local API, Egon orchestrator, local-only read/write database operations.

---

## Current Baseline

Audit source: read-only SQLite query against `C:\Users\bruno\.local\share\mouseion\refs.db` on 2026-07-03.

| Metric | Count | Percent |
| --- | ---: | ---: |
| Total refs | 249,680 | 100.00% |
| Refs with PDF marker (`pdf_local`, `pdf_path`, or `pdf_drive_id`) | 49,121 | 19.67% |
| Refs complete by Bruno's predicate | 148,308 | 59.40% |
| 80% target count | 199,744 | 80.00% |
| PDF gap to target | 150,623 | 60.33 points |
| Completion gap to target | 51,436 | 20.60 points |

High-yield buckets:

| Bucket | Count | Why it matters |
| --- | ---: | --- |
| Complete but no PDF | 115,335 | Best first PDF lane; metadata is ready, just fetch/attach PDFs. |
| DOI present but no PDF | 98,508 | Best API lane for Unpaywall, publisher, Semantic Scholar, CORE, and arXiv fallbacks. |
| OA URL present but no PDF | 7,871 | Direct-download candidates. |
| Missing publisher only for completion | 24,678 | Best metadata lane; one provider hit completes the record. |
| Missing URL/DOI only for completion | 21,280 | Good target for DOI resolver and URL normalization. |
| Missing publisher and URL/DOI | 42,919 | Needs Crossref/OpenAlex/Semantic Scholar title matching. |

Operational finding:

- Mouseion web server was not listening on `127.0.0.1:7274` during this audit.
- `enrich_queue` had `176,689 done`, `1 failed`, and no pending records, even though 51,436 more records are needed for 80% completion.
- API provider cooldowns in `api_router.db` were not active, but all budgets last moved on 2026-06-26, so the pipeline is idle rather than simply cooling down.
- Current public source was inspected at `https://github.com/outdatedcaveman/mouseion.git`, commit `276dd3d`.

## File Structure

Implement these in the Mouseion source checkout, not in Egon:

- Create `tools/mouseion_goal_audit.py`: read-only audit for the two 80% goals, outputting JSON and Markdown.
- Create `tools/mouseion_requeue_goal_gaps.py`: DB-safe queue repair; creates restore tables and requeues records that fail Bruno's predicate.
- Modify `src/mouseion/db.py`: add a single source of truth for Bruno's completion predicate and a targeted `enqueue_goal_incomplete()` method.
- Modify `src/mouseion/enrich_daemon.py`: auto-queue against Bruno's predicate when the queue is idle, not only the generic `completeness` float.
- Modify `src/mouseion/web.py`: add bounded PDF fetch configuration (`limit`, `scope`, `dry_run`) and expose goal audit status.
- Modify `src/mouseion/pdf_manager.py`: keep PDF fetching resumable and open-access-first; do not make Sci-Hub or other legally risky sources part of the default 80% run.
- Add tests under `tests/test_goal_audit.py`, `tests/test_goal_requeue.py`, and `tests/test_pdf_fetch_query.py`.

## Task 1: Baseline Goal Audit

**Owner:** Codex primary, Hermes verification.

**Files:**
- Create: `tools/mouseion_goal_audit.py`
- Test: `tests/test_goal_audit.py`

- [ ] **Step 1: Add the read-only audit script**

```python
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from pathlib import Path


def nonempty(column: str) -> str:
    return f"({column} IS NOT NULL AND TRIM(CAST({column} AS TEXT)) NOT IN ('', '[]', 'null', 'None'))"


HAS_TITLE = nonempty("title")
HAS_AUTHORS = nonempty("authors")
HAS_PUBLISHER = nonempty("publisher")
HAS_YEAR = "(year IS NOT NULL AND CAST(year AS INTEGER) > 0)"
HAS_URL_OR_DOI = f"({nonempty('url')} OR {nonempty('doi')})"
HAS_PDF = f"({nonempty('pdf_local')} OR {nonempty('pdf_path')} OR {nonempty('pdf_drive_id')})"
COMPLETE = f"({HAS_TITLE} AND {HAS_AUTHORS} AND {HAS_PUBLISHER} AND {HAS_YEAR} AND {HAS_URL_OR_DOI})"


def audit(db_path: Path) -> dict:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = dict(conn.execute(f"""
            SELECT
              COUNT(*) total,
              SUM(CASE WHEN {HAS_PDF} THEN 1 ELSE 0 END) pdf_count,
              SUM(CASE WHEN {COMPLETE} THEN 1 ELSE 0 END) complete_count,
              SUM(CASE WHEN {HAS_PDF} AND {COMPLETE} THEN 1 ELSE 0 END) both_count
            FROM refs
        """).fetchone())
        total = int(row["total"] or 0)
        target = math.ceil(total * 0.8)
        row = {k: int(v or 0) for k, v in row.items()}
        row["target_count_80"] = target
        row["pdf_pct"] = round(row["pdf_count"] * 100 / total, 2) if total else 0
        row["complete_pct"] = round(row["complete_count"] * 100 / total, 2) if total else 0
        row["pdf_gap_to_80"] = max(0, target - row["pdf_count"])
        row["complete_gap_to_80"] = max(0, target - row["complete_count"])
        row["meets_pdf_goal"] = row["pdf_count"] >= target
        row["meets_completion_goal"] = row["complete_count"] >= target
        return row
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(Path.home() / ".local" / "share" / "mouseion" / "refs.db"))
    args = parser.parse_args()
    print(json.dumps(audit(Path(args.db)), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Add a deterministic unit test**

```python
import json
import sqlite3
import subprocess
import sys


def test_goal_audit_counts(tmp_path):
    db = tmp_path / "refs.db"
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE refs (
            id TEXT PRIMARY KEY, title TEXT, authors TEXT, publisher TEXT,
            year INTEGER, url TEXT, doi TEXT, pdf_local TEXT, pdf_path TEXT, pdf_drive_id TEXT
        )
    """)
    rows = [
        ("a", "Title A", "[{}]", "Pub", 2020, "", "10/a", "a.pdf", "", ""),
        ("b", "Title B", "[{}]", "Pub", 2020, "https://b", "", "", "", ""),
        ("c", "Title C", "[{}]", "", 2020, "", "10/c", "", "", ""),
        ("d", "", "[]", "", None, "", "", "", "", ""),
    ]
    conn.executemany("INSERT INTO refs VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()

    result = subprocess.run(
        [sys.executable, "tools/mouseion_goal_audit.py", "--db", str(db)],
        check=True,
        text=True,
        capture_output=True,
    )
    payload = json.loads(result.stdout)
    assert payload["total"] == 4
    assert payload["pdf_count"] == 1
    assert payload["complete_count"] == 2
    assert payload["target_count_80"] == 4
    assert payload["pdf_gap_to_80"] == 3
    assert payload["complete_gap_to_80"] == 2
```

- [ ] **Step 3: Run the test**

Run: `pytest tests/test_goal_audit.py -q`

Expected: `1 passed`.

- [ ] **Step 4: Run live read-only audit**

Run: `python tools/mouseion_goal_audit.py --db C:\Users\bruno\.local\share\mouseion\refs.db`

Expected: JSON with nonzero `pdf_gap_to_80` and `complete_gap_to_80` until the recovery completes.

## Task 2: DB-Safe Requeue of Metadata Gaps

**Owner:** Hermes executes with backups; Codex reviews SQL.

**Files:**
- Create: `tools/mouseion_requeue_goal_gaps.py`
- Modify: `src/mouseion/db.py`
- Test: `tests/test_goal_requeue.py`

- [ ] **Step 1: Add a no-delete requeue script**

```python
from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path

from tools.mouseion_goal_audit import COMPLETE


def requeue(db_path: Path, dry_run: bool = True) -> dict:
    conn = sqlite3.connect(db_path)
    try:
        missing = conn.execute(f"SELECT COUNT(*) FROM refs WHERE NOT {COMPLETE}").fetchone()[0]
        if dry_run:
            return {"dry_run": True, "would_requeue": int(missing)}

        stamp = time.strftime("%Y%m%d_%H%M%S")
        conn.execute(f"CREATE TABLE IF NOT EXISTS enrich_queue_backup_goal_{stamp} AS SELECT * FROM enrich_queue")
        conn.execute(f"""
            INSERT INTO enrich_queue (ref_id, priority, difficulty, strategy_level, attempts, status, last_error, completeness_before, created_at)
            SELECT
                id,
                CASE
                    WHEN COALESCE(doi, '') != '' OR COALESCE(pmid, '') != '' OR COALESCE(arxiv_id, '') != '' OR COALESCE(isbn, '') != '' THEN 9.0
                    WHEN COALESCE(url, '') != '' OR COALESCE(oa_url, '') != '' THEN 4.0
                    WHEN COALESCE(title, '') != '' THEN 1.0
                    ELSE 0.05
                END,
                CASE WHEN COALESCE(doi, '') != '' OR COALESCE(pmid, '') != '' OR COALESCE(arxiv_id, '') != '' OR COALESCE(isbn, '') != '' THEN 0 ELSE 2 END,
                CASE
                    WHEN COALESCE(doi, '') != '' OR COALESCE(pmid, '') != '' OR COALESCE(arxiv_id, '') != '' OR COALESCE(isbn, '') != '' THEN 0
                    WHEN COALESCE(url, '') != '' OR COALESCE(oa_url, '') != '' THEN 1
                    WHEN COALESCE(title, '') != '' THEN 2
                    ELSE 4
                END,
                0,
                'pending',
                'goal requeue: missing Bruno completion predicate',
                COALESCE(completeness, 0),
                datetime('now')
            FROM refs
            WHERE NOT {COMPLETE}
            ON CONFLICT(ref_id) DO UPDATE SET
                status = 'pending',
                priority = MAX(enrich_queue.priority, excluded.priority),
                attempts = 0,
                last_error = excluded.last_error,
                completeness_before = excluded.completeness_before
        """)
        changed = conn.execute("SELECT changes()").fetchone()[0]
        conn.commit()
        return {"dry_run": False, "backup_table": f"enrich_queue_backup_goal_{stamp}", "requeued": int(changed)}
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(Path.home() / ".local" / "share" / "mouseion" / "refs.db"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(requeue(Path(args.db), dry_run=not args.apply))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Test dry run and apply mode on a temp DB**

Run: `pytest tests/test_goal_requeue.py -q`

Expected: dry-run reports missing rows; apply creates one `enrich_queue_backup_goal_*` table and pending queue rows without deleting any existing queue row.

- [ ] **Step 3: Live dry run**

Run: `python tools/mouseion_requeue_goal_gaps.py --db C:\Users\bruno\.local\share\mouseion\refs.db`

Expected: `would_requeue` is near the current incomplete count.

- [ ] **Step 4: Live apply only after dry-run review**

Run: `python tools/mouseion_requeue_goal_gaps.py --db C:\Users\bruno\.local\share\mouseion\refs.db --apply`

Expected: a backup table is created and pending rows appear in `enrich_queue`.

## Task 3: Make the Daemon Use Bruno's Predicate

**Owner:** Claude Code when auth recovers; Codex fallback.

**Files:**
- Modify: `src/mouseion/db.py`
- Modify: `src/mouseion/enrich_daemon.py`
- Test: `tests/test_goal_requeue.py`

- [ ] **Step 1: Add a DB method `enqueue_goal_incomplete()`**

Implement the same predicate from Task 1 inside `RefDatabase`, so the daemon is not dependent on stale `completeness` floats.

- [ ] **Step 2: Change `_auto_queue()`**

When the queue is idle, call `db.enqueue_goal_incomplete()` before `db.enqueue_incomplete(threshold=0.85, ...)`.

- [ ] **Step 3: Verify queue motion**

Run: `python run_enrichment_test.py`

Expected: pending queue decreases, `touched_10min` becomes positive, and audit completion count improves or remains explainably unchanged with provider miss evidence.

## Task 4: Bounded PDF Recovery Lane

**Owner:** Antigravity validates query/UI behavior; Hermes can run a bounded batch.

**Files:**
- Modify: `src/mouseion/web.py`
- Modify: `src/mouseion/pdf_manager.py`
- Test: `tests/test_pdf_fetch_query.py`

- [ ] **Step 1: Add bounded PDF fetch request parameters**

Allow `/api/pdfs/fetch-all` to accept JSON:

```json
{
  "scope": "complete_no_pdf",
  "limit": 1000,
  "dry_run": false
}
```

Supported scopes:

- `complete_no_pdf`: complete by Bruno's predicate and missing PDF.
- `doi_no_pdf`: DOI present and missing PDF.
- `oa_no_pdf`: OA URL or arXiv ID present and missing PDF.
- `all_supported_no_pdf`: current behavior, but bounded by `limit`.

- [ ] **Step 2: Change target query to apply `scope` and `limit`**

Expected first run query shape:

```sql
SELECT * FROM refs
WHERE (pdf_local IS NULL OR pdf_local = '')
  AND (pdf_path IS NULL OR pdf_path = '')
  AND (pdf_drive_id IS NULL OR pdf_drive_id = '')
  AND title IS NOT NULL AND TRIM(title) != ''
  AND authors IS NOT NULL AND TRIM(authors) NOT IN ('', '[]', 'null', 'None')
  AND publisher IS NOT NULL AND TRIM(publisher) != ''
  AND year IS NOT NULL AND CAST(year AS INTEGER) > 0
  AND ((url IS NOT NULL AND TRIM(url) != '') OR (doi IS NOT NULL AND TRIM(doi) != ''))
ORDER BY
  CASE
    WHEN COALESCE(oa_url, '') != '' THEN 1
    WHEN COALESCE(arxiv_id, '') != '' THEN 2
    WHEN COALESCE(doi, '') != '' THEN 3
    ELSE 4
  END,
  year DESC
LIMIT ?
```

- [ ] **Step 3: Keep default fetching open-access-first**

Do not enable Sci-Hub or other legally risky/nonstandard PDF sources as default recovery behavior. Use OA URL, arXiv, Unpaywall, Semantic Scholar open access, CORE, publisher PDF links, and already-authorized local/Drive attachments first.

- [ ] **Step 4: Verify bounded behavior**

Run a dry run through the local API after Mouseion is open:

```powershell
Invoke-RestMethod -Uri http://127.0.0.1:7274/api/pdfs/fetch-all -Method Post -ContentType application/json -Body '{"scope":"complete_no_pdf","limit":100,"dry_run":true}'
```

Expected: returns selected count and does not download or mutate records.

## Task 5: Daily Evidence Loop

**Owner:** Hermes automation; Codex owns Egon memory.

**Files:**
- Create: `tools/mouseion_daily_goal_report.py`

- [ ] **Step 1: Run goal audit before and after each batch**

Run:

```powershell
python tools/mouseion_goal_audit.py --db C:\Users\bruno\.local\share\mouseion\refs.db
```

- [ ] **Step 2: Write an Egon memory after each material run**

Call `/api/v1/mind/memory/upsert` with:

```json
{
  "kind": "fact",
  "project": "mouseion",
  "content": "Mouseion goal run: before/after PDF and completion counts, batch size, scopes, files/endpoints touched, verification, remaining gap.",
  "tags": ["mouseion", "pdf", "metadata", "80-percent-goal"]
}
```

- [ ] **Step 3: Stop only on proven risk**

Stop and ask Bruno before irreversible deletion, public exposure, credential changes, or any source that would violate platform/legal rules.

## Cross-Agent Deployment

Create these orchestrator tasks:

- Codex: implement/review Tasks 1, 2, and cross-agent progress reporting.
- Hermes: run read-only audit, run dry-run queue repair, then apply only after a backup table is confirmed.
- Antigravity: validate Task 4 PDF query/API behavior and inspect UI progress surfaces.
- Claude Code: when auth recovers, implement Task 3 daemon changes and tests; if still unavailable, Codex owns fallback.

Each agent must append task events before and after meaningful steps and write compact durable memory with counts, commands, files touched, and remaining risk.

## Deployment Update - 2026-07-03 Codex Task 44

Task 4 source changes were deployed in `C:\Users\bruno\AI\checkouts\mouseion` on branch `codex/task-53-goal-tooling`:

- `src/mouseion/web.py`: `/api/pdfs/fetch-all` now accepts `scope`, `limit`, `dry_run`, and `allow_gray_sources`. Default request behavior is bounded to `scope=complete_no_pdf`, `limit=1000`, `dry_run=false`, and `allow_gray_sources=false`. Dry runs return `selected_count` without starting a worker or mutating records.
- `src/mouseion/web.py`: target selection now uses the Bruno completion/PDF predicates from `src/mouseion/db.py`, includes `pdf_path` in missing-PDF detection, orders open-access URLs first, then arXiv, DOI, and title candidates, and applies `LIMIT ?`.
- `src/mouseion/pdf_manager.py`: Sci-Hub and Anna's Archive are no longer default recovery sources. They are only attempted when `allow_gray_sources=true` is passed explicitly.
- `tests/test_pdf_fetch_query.py`: covers scoped target selection, bounded defaults, and the gray-source opt-in policy.

Verification performed:

- `python -m pytest tests/test_goal_audit.py tests/test_goal_requeue.py tests/test_pdf_fetch_query.py -q` -> `9 passed in 1.56s`.
- `python -m compileall src\mouseion\web.py src\mouseion\pdf_manager.py` -> both files compiled.
- `python tools/mouseion_goal_audit.py --db C:\Users\bruno\.local\share\mouseion\refs.db` -> total `249680`, PDFs `49121` (`19.67%`), complete `148308` (`59.4%`), PDF gap `150623`, completion gap `51436`.
- Read-only scoped query check with `PYTHONPATH=C:\Users\bruno\AI\checkouts\mouseion\src` -> `complete_no_pdf`, `doi_no_pdf`, `oa_no_pdf`, and `all_supported_no_pdf` each returned `selected_count=1000` with `limit=1000`.

Remaining risk:

- `http://127.0.0.1:7274` was offline during verification, so live HTTP dry-run verification of `/api/pdfs/fetch-all` is still pending.
- No live `refs.db` mutation was performed in this task. Hermes or another runner should start Mouseion locally, call the API with `{"scope":"complete_no_pdf","limit":1000,"dry_run":true}`, then run a small non-dry-run batch only after confirming the response and status surfaces.
- The live goal counts have not improved yet; this task deployed the bounded/legal PDF recovery lane needed to start increasing the PDF percentage safely.

## Deployment Update - 2026-07-03 Codex Task 49

Current verification:

- `python tools/mouseion_goal_audit.py --db C:\Users\bruno\.local\share\mouseion\refs.db` -> total `249680`, PDFs `49121` (`19.67%`), complete `148308` (`59.4%`), PDF gap `150623`, completion gap `51436`.
- `python -m pytest tests/test_goal_audit.py tests/test_goal_requeue.py tests/test_pdf_fetch_query.py -q` -> `9 passed in 1.22s`.
- `python tools/mouseion_requeue_goal_gaps.py --db C:\Users\bruno\.local\share\mouseion\refs.db` -> dry run `would_requeue=101372`.

Deployment status:

- Done: baseline audit tooling, safe requeue tooling, Bruno completion predicate in `src/mouseion/db.py`, daemon auto-queue use of that predicate, bounded `/api/pdfs/fetch-all`, and open-access-first PDF source policy.
- Still pending: live Mouseion HTTP dry-run because `127.0.0.1:7274` has been offline in all verification passes; live metadata queue apply; small bounded PDF batch; UI/status validation; final merge/review of the dirty Mouseion checkout.

Next orchestrator wave:

- Hermes: apply metadata queue repair only through `tools/mouseion_requeue_goal_gaps.py --apply`, relying on the script-created `enrich_queue_backup_goal_<timestamp>` restore table; then run a read-only audit and queue status check.
- Claude Code: review and stabilize the daemon predicate path and merge readiness. If Claude auth still fails, the orchestrator should reroute to Codex.
- Antigravity: validate the Mouseion web UI/status surfaces and `/api/pdfs/fetch-all` dry-run from a running local server. If Antigravity cannot run, the orchestrator should reroute to Codex.
- Codex: handle rerouted validation, review diff/test output, and keep Egon memory current with counts, commands, changed files, and remaining risk.

Operational guardrails:

- No permanent deletion. Any live DB mutation must be restorable from a same-run backup table.
- Default PDF recovery must keep `allow_gray_sources=false`; Sci-Hub/Anna's Archive require explicit opt-in and are not part of the default 80% run.
- Each batch must publish before/after goal counts. Stop if the PDF or metadata counts regress, if the DB backup table is missing, or if the local HTTP dry-run cannot prove bounded target selection.
