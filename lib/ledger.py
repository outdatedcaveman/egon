"""Real ledger — parses ~/.claude/projects/*/*.jsonl session transcripts.

Each assistant turn carries a `message.usage` block:
    input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens
plus `message.model` and `timestamp` (ISO Z) and `cwd` (project attribution).

Plan-mode awareness:
- `pro`  → headline = tokens & plan-budget overlay; cost is shown as "API counterfactual" only.
- `max`  → same as pro but higher limit.
- `api`  → cost is the headline.
The setting lives in egon-config.json (`plan_mode` key); default `pro`.
"""
from __future__ import annotations

import glob
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Literal

from lib.pricing import PRICING

PROJECTS_GLOB = os.path.expanduser("~/.claude/projects/*/*.jsonl")
_DB_PATH = Path(__file__).resolve().parent.parent / "state" / "mind.db"

# tiny in-process cache: (range_key, plan_mode) -> ((max_mtime_seen, num_files), computed_ledger)
_CACHE: dict[tuple[str, str], tuple[tuple[float, int], dict]] = {}

PlanMode = Literal["pro", "max", "api"]

# Approximate plan limits (per Anthropic's published Pro/Max policies; updated when they change).
# These are tokens-per-month soft caps for typical usage. Used only as a "% of plan" gauge.
PLAN_LIMITS = {
    "pro": 50_000_000,    # ~50M tokens/month soft cap (Pro $20/mo, Sonnet+Opus mix)
    "max": 250_000_000,   # ~250M tokens/month (Max $200/mo)
    "api": None,          # no cap; cost is what matters
}

# UI projects we care about (mapped from cwd). Order matters: most-specific first.
PROJECT_MAP: list[tuple[str, str]] = [
    # explicit subdir matches
    ("claude code\\noiacast",      "Noiacast"),
    ("claude code\\egon-local",    "Egon"),
    ("claude code\\egon",          "Egon"),
    ("claude code\\claude-meta",   "claude-meta"),
    ("careerops",                  "CareerOps"),
    ("panop",                      "Panop"),
    ("noiacast",                   "Noiacast"),
    ("carrera",                    "Carrera"),
    ("kms_auto_router",            "Routster"),
    ("zoterpile",                  "Mouseion"),
    ("\\.claude-mem\\",            "claude-mem"),
    # fallback: top-level Claude Code with no project subdir → general dev work
    ("claude code",                "Claude Code (general)"),
]


# ---- helpers ---------------------------------------------------------------

def _project_for_cwd(cwd: str | None, session_path: str | None = None) -> str:
    """Map cwd → project label. Fallback to the JSONL folder slug if cwd is generic."""
    candidates: list[str] = []
    if cwd:
        candidates.append(cwd.lower().replace("/", "\\"))
    if session_path:
        # ~/.claude/projects/C--Users-bruno-Claude-Code-egon/<id>.jsonl
        slug = Path(session_path).parent.name
        # decode: leading "C--" → "c:\", "-" → "\", but only after the C-- prefix
        # simpler: look for "claude-code-<X>" or "Claude-Code-<X>" markers in the slug
        candidates.append(slug.lower().replace("-", "\\"))

    for hay in candidates:
        for needle, label in PROJECT_MAP:
            if needle in hay:
                return label
        # generic "claude code\X" → take next path segment
        if "claude code\\" in hay:
            rest = hay.split("claude code\\", 1)[1].split("\\")[0]
            if rest:
                return rest.title() if len(rest) <= 20 else rest
        if "claude\\code\\" in hay:
            rest = hay.split("claude\\code\\", 1)[1].split("\\")[0]
            if rest:
                return rest.title()

    # if cwd is just the user's home folder → general/home sessions
    user_home = os.path.expanduser("~").lower()
    if cwd and cwd.rstrip("\\").lower() in (user_home, user_home + "\\"):
        return "home"

    return "other"


def _model_code(model: str) -> str:
    m = (model or "").lower()
    if "opus" in m:   return "opus"
    if "sonnet" in m: return "sonnet"
    if "haiku" in m:  return "haiku"
    return "other"


def _model_pricing_key(code: str) -> str | None:
    return {"opus": "opus-4-7", "sonnet": "sonnet-4-6", "haiku": "haiku-4-5"}.get(code)


def _api_cost(code: str, u: dict) -> float:
    key = _model_pricing_key(code)
    if not key:
        return 0.0
    pi, po, pcw, pcr = PRICING[key]
    return (
        u.get("input_tokens", 0)               * pi  / 1_000_000
        + u.get("output_tokens", 0)            * po  / 1_000_000
        + u.get("cache_creation_input_tokens", 0) * pcw / 1_000_000
        + u.get("cache_read_input_tokens", 0)    * pcr / 1_000_000
    )


# ---- iterator ---------------------------------------------------------------

def _tool_names_from_content(content) -> list[str]:
    """Extract tool names invoked in this turn. content can be list[dict] or str."""
    if not isinstance(content, list):
        return []
    names = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use":
            n = block.get("name")
            if n:
                names.append(n)
    return names


def _iter_turns(path_glob: str = PROJECTS_GLOB) -> Iterable[dict]:
    """Yield (timestamp, project, model_code, usage, tool_names, file_path) per assistant turn."""
    for path in glob.glob(path_glob):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if d.get("type") != "assistant":
                        continue
                    msg = d.get("message") or {}
                    usage = msg.get("usage") or {}
                    if not usage:
                        continue
                    ts = d.get("timestamp")
                    if not ts:
                        continue
                    try:
                        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    yield {
                        "ts": dt,
                        "project": _project_for_cwd(d.get("cwd"), path),
                        "model": _model_code(msg.get("model", "")),
                        "usage": usage,
                        "tools": _tool_names_from_content(msg.get("content")),
                        "session": Path(path).stem,
                        "session_path": path,
                    }
        except OSError:
            continue


def _iter_db_turns(active_session_uuids: set[str]) -> Iterable[dict]:
    """Yield database-archived turns, excluding any active sessions currently on disk."""
    if not _DB_PATH.exists():
        return
    import sqlite3
    try:
        conn = sqlite3.connect(_DB_PATH, timeout=5)
        conn.row_factory = sqlite3.Row
        query = """
            SELECT t.ts, t.model, t.input_tokens, t.output_tokens, t.cache_write_tokens, t.cache_read_tokens, t.tools,
                   s.external_id, p.slug as project_slug
            FROM turns_ledger t
            JOIN sessions s ON s.id = t.session_id
            LEFT JOIN projects p ON p.id = s.project_id
        """
        rows = conn.execute(query).fetchall()
        conn.close()
    except Exception:
        return

    for row in rows:
        uuid = row["external_id"]
        if uuid in active_session_uuids:
            continue
        try:
            dt = datetime.fromtimestamp(row["ts"], tz=timezone.utc)
        except ValueError:
            continue
        yield {
            "ts": dt,
            "project": row["project_slug"] or "other",
            "model": _model_code(row["model"]),
            "usage": {
                "input_tokens": row["input_tokens"],
                "output_tokens": row["output_tokens"],
                "cache_creation_input_tokens": row["cache_write_tokens"],
                "cache_read_input_tokens": row["cache_read_tokens"],
            },
            "tools": [t.strip() for t in (row["tools"] or "").split(",") if t.strip()],
            "session": uuid,
            "session_path": None,
        }


# tool name → friendly label + kind
_TOOL_LABEL: dict[str, tuple[str, str]] = {
    "Bash":              ("Bash",                "Built-in"),
    "Read":              ("Read",                "Built-in"),
    "Write":             ("Write",               "Built-in"),
    "Edit":              ("Edit",                "Built-in"),
    "Grep":              ("Grep",                "Built-in"),
    "Glob":              ("Glob",                "Built-in"),
    "WebFetch":          ("WebFetch",            "Built-in"),
    "WebSearch":         ("WebSearch",           "Built-in"),
    "TodoWrite":         ("TodoWrite",           "Built-in"),
    "PowerShell":        ("PowerShell",          "Built-in"),
    "Agent":             ("Agent",               "Built-in"),
    "Task":              ("Task",                "Built-in"),
    "NotebookEdit":      ("NotebookEdit",        "Built-in"),
}


def _label_tool(name: str) -> tuple[str, str]:
    if name in _TOOL_LABEL:
        return _TOOL_LABEL[name]
    if name.startswith("mcp__"):
        # mcp__<server>__<tool> → strip server hash, keep tool name
        parts = name.split("__")
        if len(parts) >= 3:
            return (parts[-1], f"MCP · {parts[1][:18]}")
        return (name, "MCP")
    if name.startswith("Skill"):
        return (name, "Skill")
    return (name, "Tool")


# ---- aggregator -------------------------------------------------------------

def compute_ledger(plan_mode: PlanMode = "pro",
                   range_key: str = "30d",
                   path_glob: str = PROJECTS_GLOB,
                   force: bool = False) -> dict:
    # cache check: skip recompute if no JSONL has been modified since last run
    files = glob.glob(path_glob)
    max_mtime = max((os.path.getmtime(p) for p in files), default=0.0)
    cache_state = (max_mtime, len(files))
    cache_key = (range_key, plan_mode)
    if not force and cache_key in _CACHE:
        cached_state, cached = _CACHE[cache_key]
        if cached_state == cache_state:
            return cached

    now_local = datetime.now().astimezone()
    now = now_local.astimezone(timezone.utc)
    today = now_local.date()
    last_month_start = (now_local.replace(day=1) - timedelta(days=1)).replace(day=1).date()
    this_month_start = now_local.replace(day=1).date()

    cutoffs = {
        "24h": now - timedelta(hours=24),
        "7d":  now - timedelta(days=7),
        "30d": now - timedelta(days=30),
        "90d": now - timedelta(days=90),
        "ytd": datetime(now.year, 1, 1, tzinfo=timezone.utc),
        "all": datetime(2020, 1, 1, tzinfo=timezone.utc),
    }
    cutoff = cutoffs.get(range_key, cutoffs["30d"])

    today_cost = today_in = today_out = today_cw = today_cr = 0.0
    mtd_cost = 0.0
    mtd_tokens = 0
    mtd_in = mtd_out = mtd_cw = mtd_cr = 0
    last_month_tokens = 0
    last_month_cost = 0.0
    range_input = range_output = range_cw = range_cr = 0
    range_cost_api = 0.0

    # rolling 24h burn (timestamp-based, not day-bucketed)
    cutoff_24h = now - timedelta(hours=24)
    rolling_24h_tokens = 0
    rolling_24h_turns = 0

    # verification — proof of access
    total_turns = 0
    sessions_seen: set[str] = set()
    last_turn_ts: datetime | None = None

    by_model: dict[str, dict[str, float]] = defaultdict(lambda: {"cost": 0.0, "tokens": 0})
    by_project: dict[str, dict[str, float]] = defaultdict(lambda: {"cost": 0.0, "tokens": 0})
    by_day: dict[str, dict[str, float]] = defaultdict(lambda: {"input": 0, "output": 0, "cw": 0, "cr": 0})
    by_tool: dict[str, dict[str, float]] = defaultdict(lambda: {"calls": 0, "cost_share": 0.0, "turns": 0})
    sessions: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "project": "?", "model": "?", "input": 0, "output": 0, "cw": 0, "cr": 0,
        "cost": 0.0, "first_ts": None, "last_ts": None,
    })

    range_turns_count = 0

    active_session_uuids = {Path(p).stem for p in files}
    import itertools
    all_turns = itertools.chain(_iter_turns(path_glob), _iter_db_turns(active_session_uuids))

    for t in all_turns:
        ts: datetime = t["ts"]
        ts_local = ts.astimezone()
        u = t["usage"]
        proj = t["project"]
        model = t["model"]

        i_  = u.get("input_tokens", 0) or 0
        o_  = u.get("output_tokens", 0) or 0
        cw  = u.get("cache_creation_input_tokens", 0) or 0
        cr  = u.get("cache_read_input_tokens", 0) or 0
        cost = _api_cost(model, u)
        tot = i_ + o_ + cw + cr

        # This month
        if ts_local.date() >= this_month_start:
            mtd_cost += cost
            mtd_tokens += tot
            mtd_in += i_; mtd_out += o_; mtd_cw += cw; mtd_cr += cr
        elif ts_local.date() >= last_month_start:
            last_month_cost += cost
            last_month_tokens += tot

        # Today (local time)
        if ts_local.date() == today:
            today_cost += cost
            today_in   += i_
            today_out  += o_
            today_cw   += cw
            today_cr   += cr

        # Rolling 24h burn rate
        if ts >= cutoff_24h:
            rolling_24h_tokens += tot
            rolling_24h_turns += 1

        # Verification
        total_turns += 1
        sessions_seen.add(t["session"])
        if last_turn_ts is None or ts > last_turn_ts:
            last_turn_ts = ts

        # Range (drives the chart, by-model, by-project, sessions table)
        if ts >= cutoff:
            range_input  += i_
            range_output += o_
            range_cw     += cw
            range_cr     += cr
            range_cost_api += cost
            by_model[model]["cost"]   += cost
            by_model[model]["tokens"] += i_ + o_ + cw + cr
            by_project[proj]["cost"]   += cost
            by_project[proj]["tokens"] += i_ + o_ + cw + cr
            day = ts.date().isoformat()
            by_day[day]["input"]  += i_
            by_day[day]["output"] += o_
            by_day[day]["cw"]     += cw
            by_day[day]["cr"]     += cr

            s = sessions[t["session"]]
            s["project"] = proj
            s["model"]   = model
            s["input"]  += i_
            s["output"] += o_
            s["cw"]     += cw
            s["cr"]     += cr
            s["cost"]   += cost
            s["first_ts"] = min(s["first_ts"], ts) if s["first_ts"] else ts
            s["last_ts"]  = max(s["last_ts"], ts)  if s["last_ts"]  else ts
            range_turns_count += 1

            # tool_use aggregation — attribute this turn's cost equally across tools invoked
            tools = t.get("tools", [])
            if tools:
                per_tool_cost = cost / len(tools)
                for tn in tools:
                    by_tool[tn]["calls"] += 1
                    by_tool[tn]["cost_share"] += per_tool_cost
                by_tool[tools[0]]["turns"] += 1  # count turn once even if multi-tool

    # ---- shape into the schema the UI expects ----
    today_total = today_in + today_out + today_cw + today_cr
    today_cache_hit = (today_cr / (today_cr + today_in + today_cw)) if (today_cr + today_in + today_cw) > 0 else 0.0
    range_cache_hit = (range_cr / (range_cr + range_input + range_cw)) if (range_cr + range_input + range_cw) > 0 else 0.0

    # 7-day average (for delta vs today)
    seven_days_back = today - timedelta(days=7)
    last7 = [v for k, v in by_day.items() if k >= seven_days_back.isoformat() and k != today.isoformat()]
    avg7 = sum(_api_cost_from_day(d) for d in last7) / max(len(last7), 1)
    today_vs_avg = today_cost - avg7

    # Burn rate: real rolling 24h window from raw timestamps (not bucketed days)
    burn_per_hr = int(rolling_24h_tokens / 24) if rolling_24h_tokens else 0

    # Stacked chart points (cumulative not — raw layer values per day, in millions)
    dates_sorted = sorted(by_day.keys())
    if not dates_sorted:
        dates_sorted = [today.isoformat()]
    # downsample to <= 30 points for the chart, take last N
    keep = dates_sorted[-30:]
    labels = [keep[0], keep[len(keep)//2] if len(keep) > 1 else keep[0], keep[-1]] if len(keep) > 2 else keep
    stacked = {
        "labels": labels,
        "cache_reads":  [round(by_day[d]["cr"]/1_000_000, 3) for d in keep],
        "cache_writes": [round(by_day[d]["cw"]/1_000_000, 3) for d in keep],
        "input":        [round(by_day[d]["input"]/1_000_000, 3) for d in keep],
        "output":       [round(by_day[d]["output"]/1_000_000, 3) for d in keep],
    }

    # By-model / by-project shares
    bm_total = sum(v["cost"] for v in by_model.values()) or 1.0
    bm_list = []
    for code in ("opus", "sonnet", "haiku"):
        if code in by_model:
            bm_list.append({
                "model": {"opus": "Opus 4.7", "sonnet": "Sonnet 4.6", "haiku": "Haiku 4.5"}[code],
                "code":  code,
                "cost_usd": round(by_model[code]["cost"], 2),
                "tokens":   by_model[code]["tokens"],
                "share":    round(by_model[code]["cost"] / bm_total, 4),
            })

    bp_total = sum(v["cost"] for v in by_project.values()) or 1.0
    bp_list = sorted(
        [{"project": k, "cost_usd": round(v["cost"], 2), "tokens": v["tokens"],
          "share": round(v["cost"]/bp_total, 4)} for k, v in by_project.items()],
        key=lambda r: r["cost_usd"], reverse=True,
    )[:8]

    # Recent sessions — last 8 by last_ts
    sess_list = sorted(sessions.values(), key=lambda s: s["last_ts"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)[:8]
    sess_out = []
    for s in sess_list:
        hit = s["cr"] / (s["cr"] + s["input"] + s["cw"]) if (s["cr"] + s["input"] + s["cw"]) > 0 else 0
        sess_out.append({
            "time": s["last_ts"].astimezone().strftime("%Y-%m-%d %H:%M") if s["last_ts"] else "—",
            "project": s["project"],
            "model": s["model"],
            "input": s["input"],
            "output": s["output"],
            "cache_read": s["cr"],
            "cache_write": s["cw"],
            "hit_pct": round(hit * 100),
            "cost_usd": round(s["cost"], 2),
        })

    # Projection — month-end at current pace
    if now_local.month == 12:
        days_in_month = 31
    else:
        days_in_month = (datetime(now_local.year, now_local.month + 1, 1) -
                         timedelta(days=1)).day
    day_of_month = max(now_local.day, 1)
    proj_month_cost = (mtd_cost / day_of_month) * days_in_month
    proj_month_tokens = int((mtd_tokens / day_of_month) * days_in_month)

    # Counterfactual (range-scoped, for the projection card next to the chart)
    cf_no_cache = 0.0
    for code, agg in by_model.items():
        key = _model_pricing_key(code)
        if not key:
            continue
        pi, po, _, _ = PRICING[key]
        share = (agg["cost"] / range_cost_api) if range_cost_api > 0 else 0
        cf_no_cache += (range_input + range_cr + range_cw) * share * pi / 1_000_000
        cf_no_cache += range_output * share * po / 1_000_000
    cache_savings_pct = round(100 * (1 - range_cost_api / cf_no_cache)) if cf_no_cache > 0 else 0

    # Counterfactual (MTD-scoped, for the KPI strip — must match its MTD neighbor)
    # We use an aggregate-pricing trick: weight by the model mix observed in MTD's by_model.
    # Approximation: assume MTD model mix == range model mix (true within ~95% in practice).
    mtd_cf_no_cache = 0.0
    if mtd_in + mtd_cr + mtd_cw + mtd_out > 0 and range_cost_api > 0:
        for code, agg in by_model.items():
            key = _model_pricing_key(code)
            if not key: continue
            pi, po, _, _ = PRICING[key]
            share = agg["cost"] / range_cost_api
            mtd_cf_no_cache += (mtd_in + mtd_cr + mtd_cw) * share * pi / 1_000_000
            mtd_cf_no_cache += mtd_out * share * po / 1_000_000
    mtd_saved_usd = max(mtd_cf_no_cache - mtd_cost, 0.0)
    mtd_cache_savings_pct = round(100 * (1 - mtd_cost / mtd_cf_no_cache)) if mtd_cf_no_cache > 0 else 0

    # Pro-mode "vs last month" trend (more meaningful than fake plan-limit gauge)
    vs_last_month_pct = None
    if last_month_tokens > 0:
        # compare same-day-of-month for fairness
        ratio = mtd_tokens / max(last_month_tokens * (day_of_month / days_in_month), 1)
        vs_last_month_pct = round((ratio - 1) * 100)

    # Anomaly: if today's tokens > 1.8× rolling 7-day avg
    avg7_tokens = sum((v["input"]+v["output"]+v["cw"]+v["cr"]) for k, v in by_day.items()
                      if k >= seven_days_back.isoformat() and k != today.isoformat()) / max(len(last7), 1)
    anomaly = None
    if today_total > 1.8 * avg7_tokens and avg7_tokens > 0:
        anomaly = {
            "level": "warn",
            "headline": f"Today's usage is {today_total/avg7_tokens:.1f}× your 7-day average.",
            "driver": f"{total_turns} assistant turns today across {len(by_project)} projects.",
            "suggestion": "Re-warm context cache and consolidate runs to reduce cache_creation churn.",
        }

    result = {
        "plan_mode": plan_mode,
        "range": range_key,
        "today_cost_usd":      round(today_cost, 2),
        "today_tokens":        today_total,
        "mtd_cost_usd":        round(mtd_cost, 2),
        "mtd_tokens":          mtd_tokens,
        "mtd_saved_usd":       round(mtd_saved_usd, 2),
        "mtd_cache_savings_pct": mtd_cache_savings_pct,
        "burn_rate_per_hour":  burn_per_hr,
        "burn_rate_24h_turns": rolling_24h_turns,
        "cache_hit_ratio":     round(today_cache_hit, 3) if today_total else round(range_cache_hit, 3),
        "cache_hit_delta_pct": 0,
        "today_vs_7d_avg_usd": round(today_vs_avg, 2),
        "anomaly":             anomaly,
        "verification": {
            "files_parsed":      len(files),
            "total_turns_ever":  total_turns,
            "sessions_ever":     len(sessions_seen),
            "range_turns":       range_turns_count,
            "last_turn_iso":     last_turn_ts.isoformat() if last_turn_ts else None,
        },
        "today_spark":         [round(by_day[d]["input"]+by_day[d]["output"]+by_day[d]["cw"]+by_day[d]["cr"], 0)
                                for d in dates_sorted[-8:]] or [0],
        "stacked_30d":         stacked,
        "projection": {
            "month_end_cost_usd": round(proj_month_cost, 2),
            "month_end_tokens":   proj_month_tokens,
            "without_cache_usd":  round(cf_no_cache, 2),
            "cache_savings_pct":  cache_savings_pct,
        },
        "by_model":   bm_list,
        "by_project": bp_list,
        "recent_sessions": sess_out,
        "top_skills": _top_skills(by_tool),
        "plan_budget": {
            "mode":  plan_mode,
            "vs_last_month_pct":  vs_last_month_pct,
            "last_month_tokens":  last_month_tokens,
            "this_month_tokens":  mtd_tokens,
        },
    }
    _CACHE[cache_key] = (cache_state, result)
    return result


def _top_skills(by_tool: dict[str, dict]) -> list[dict]:
    """Top tools/skills by attributed cost. Returns up to 10."""
    rows = []
    for name, agg in by_tool.items():
        label, kind = _label_tool(name)
        rows.append({
            "name": label,
            "raw_name": name,
            "kind": kind,
            "subtitle": f"{agg['calls']} invocations",
            "calls": agg["calls"],
            "cost_usd": round(agg["cost_share"], 2),
        })
    rows.sort(key=lambda r: r["cost_usd"], reverse=True)
    return rows[:10]


def _api_cost_from_day(d: dict) -> float:
    """Fallback cost estimate from a daily aggregate (assume Sonnet rates)."""
    return (
        d["input"]  * 3.00 / 1_000_000
        + d["output"] * 15.00 / 1_000_000
        + d["cw"]   * 3.75 / 1_000_000
        + d["cr"]   * 0.30 / 1_000_000
    )


# ---- config ----------------------------------------------------------------

CONFIG_PATH = Path(__file__).resolve().parent.parent / "egon-config.json"
DEFAULT_CONFIG = {"plan_mode": "pro", "dark_mode": False}


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            from lib.secrets import decrypt_dict
            decrypted = decrypt_dict(raw)
            return {**DEFAULT_CONFIG, **decrypted}
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)


def save_config(cfg: dict) -> None:
    from lib.secrets import encrypt_dict
    encrypted = encrypt_dict(cfg)
    CONFIG_PATH.write_text(json.dumps(encrypted, indent=2), encoding="utf-8")
