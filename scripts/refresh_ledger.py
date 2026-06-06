"""Recompute the ledger and merge it into Egon's last_pass.json files.

Used by:
- The dashboard when a lightweight ledger refresh is needed.
- Scheduled refresh tasks.
- Manual: python scripts/refresh_ledger.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib.ledger import compute_ledger, load_config  # noqa: E402
from lib.state import LAST_PASS_CANDIDATES  # noqa: E402


def _newest_last_pass() -> Path | None:
    candidates = [p for p in LAST_PASS_CANDIDATES if p.exists() and p.stat().st_size > 0]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _write_all(payload: dict) -> tuple[list[Path], list[str]]:
    written: list[Path] = []
    warnings: list[str] = []
    for target in LAST_PASS_CANDIDATES:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(target)
            written.append(target)
        except Exception as e:
            warnings.append(f"{target}: {e}"[:260])
    return written, warnings


def main() -> int:
    cfg = load_config()
    range_key = sys.argv[1] if len(sys.argv) > 1 else "30d"
    ledger = compute_ledger(plan_mode=cfg.get("plan_mode", "pro"), range_key=range_key)

    data = {"schema_version": "0.2.0", "_partial": True}
    existing = _newest_last_pass()
    if existing:
        try:
            data = json.loads(existing.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print("warn: existing last_pass.json invalid, starting fresh")

    data["ledger"] = ledger
    written, warnings = _write_all(data)
    if warnings:
        print("write warnings:")
        for warning in warnings:
            print(f"  {warning}")
    if not written:
        print("error: no last_pass target could be written")
        return 1

    print(
        f"wrote ledger ({range_key}): "
        f"{ledger['mtd_tokens']:,} MTD tokens, "
        f"${ledger['mtd_cost_usd']} api-equiv, "
        f"vs last month {ledger['plan_budget']['vs_last_month_pct']:+}%"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
