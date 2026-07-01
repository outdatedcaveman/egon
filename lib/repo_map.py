"""Project → repo directory map, for parameter-level code access in Egon Chat.

Bruno wants the orchestrator chat to reason about his ACTUAL code — functions,
parameters, config — not just rollout summaries. That needs to know where each
project lives on disk. The mind's projects.root_path is empty and the repos are
scattered (egon under Claude Code, mouseion under Documents/New project, …), so
we build the map two ways and let Bruno correct it:

  1. Auto-discover: scan the known code roots for directories whose name matches
     a canonical project slug (or alias), and mine recent AI session logs
     (.codex/.claude) for the `cwd` each project was worked in.
  2. Override: state/project_repos.json — an editable {slug: path} Bruno owns.
     Overrides always win; discovery only fills gaps.

Read-only consumer side (`repo_files_for`) greps a repo for query terms and
returns ranked snippets to inject as context. Bruno 2026-07-01.
"""
from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path

from lib import egon_paths

HOME = Path.home()
OVERRIDES = egon_paths.STATE_DIR / "project_repos.json"

# Where Bruno's repos live. First match wins for a given slug.
_CODE_ROOTS = [
    HOME / "Claude Code",
    HOME / "Workspace",
    HOME / "Documents",
    HOME / "source" / "repos",
    HOME / "Projects",
]

# Dirs that are never a project repo (and are skipped when ranking files).
_SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".idea", ".vscode",
             "venv", "env", "dist", "build", ".mypy_cache", ".pytest_cache",
             "state", "logs", ".backups", "backups", "restore_points",
             "index_backups", "config_backup", ".cache", "site-packages",
             # bundled/browser/output artefacts that drown out real source
             "shell", "profile", "extensions", "panop_output", "output",
             "coverage", "htmlcov", "assets", "static", "vendor", "third_party",
             ".next", ".turbo", "target", "bin", "obj"}

# Slugs that resolve to a code-root or the home dir itself — not real projects.
_NON_PROJECT_SLUGS = {"bruno", "claude code", "claude-code", "documents", "desktop",
                      "downloads", "workspace", "projects", "users", "home", "data",
                      "source", "repos", "new-project", "new project"}
_CODE_EXT = {".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rs", ".c",
             ".cpp", ".h", ".hpp", ".cs", ".rb", ".php", ".swift", ".kt",
             ".md", ".json", ".toml", ".yaml", ".yml", ".sql", ".sh", ".bat"}


def _load_overrides() -> dict[str, str]:
    try:
        if OVERRIDES.exists():
            d = json.loads(OVERRIDES.read_text(encoding="utf-8"))
            return {str(k).lower(): str(v) for k, v in (d or {}).items()
                    if v and not str(k).startswith("_")}
    except Exception:
        pass
    return {}


def save_override(slug: str, path: str) -> None:
    """Bruno-facing: pin a project's repo path (wins over discovery)."""
    d = _load_overrides()
    d[slug.lower()] = path
    try:
        OVERRIDES.parent.mkdir(parents=True, exist_ok=True)
        OVERRIDES.write_text(json.dumps(d, indent=2), encoding="utf-8")
    except Exception:
        pass
    discover.cache_clear()


def _slug_aliases() -> dict[str, str]:
    try:
        from lib.mind_project_resolver import known_project_slugs
        return {s: s for s in known_project_slugs()}
    except Exception:
        return {}


def _scan_cwd_from_sessions(limit_files: int = 60) -> dict[str, str]:
    """Mine recent Codex/Claude session logs for the working directory each
    project was edited in (they record `cwd`). Cheap, best-effort."""
    out: dict[str, str] = {}
    try:
        from lib.mind_project_resolver import canonical_slug
    except Exception:
        return out
    roots = [HOME / ".codex" / "sessions", HOME / ".claude" / "projects"]
    files: list[Path] = []
    for r in roots:
        if r.exists():
            files += sorted(r.rglob("*.jsonl"),
                            key=lambda p: p.stat().st_mtime, reverse=True)[:limit_files]
    cwd_re = re.compile(r'"cwd"\s*:\s*"([^"]+)"')
    for f in files[:limit_files]:
        try:
            head = f.read_text(encoding="utf-8", errors="ignore")[:4000]
        except Exception:
            continue
        m = cwd_re.search(head)
        if not m:
            continue
        raw = m.group(1).replace("\\\\", "\\").replace("\\\\?\\", "").lstrip("\\?")
        p = Path(raw)
        if not p.exists():
            continue
        slug = canonical_slug(str(p)) or canonical_slug(p.name)
        if slug and slug not in out:
            out[slug] = str(p)
    return out


@lru_cache(maxsize=1)
def discover() -> dict[str, str]:
    """Return {slug: repo_path}. Overrides win; then on-disk name matches; then
    session cwds. Cached — call discover.cache_clear() after editing overrides."""
    known = set(_slug_aliases().keys())
    roots_resolved = {str(r.resolve()).lower() for r in _CODE_ROOTS if r.exists()}
    roots_resolved.add(str(HOME.resolve()).lower())

    def _acceptable(slug: str | None, path: Path) -> bool:
        if not slug or slug in _NON_PROJECT_SLUGS or slug not in known:
            return False
        try:
            if str(path.resolve()).lower() in roots_resolved:
                return False  # the code-root / home dir itself is not a project
        except Exception:
            pass
        return True

    found: dict[str, str] = {}
    # 1) name matches under code roots (one level deep)
    for root in _CODE_ROOTS:
        if not root.exists():
            continue
        try:
            for child in root.iterdir():
                if not child.is_dir() or child.name in _SKIP_DIRS:
                    continue
                try:
                    from lib.mind_project_resolver import canonical_slug
                    slug = canonical_slug(child.name)
                except Exception:
                    slug = child.name.lower()
                if _acceptable(slug, child) and slug not in found:
                    found[slug] = str(child)
        except Exception:
            continue
    # 2) session cwds fill gaps (esp. repos outside the standard roots)
    for slug, path in _scan_cwd_from_sessions().items():
        if _acceptable(slug, Path(path)):
            found.setdefault(slug, path)
    # 3) overrides win (Bruno's corrections; not filtered)
    found.update(_load_overrides())
    return found


def repo_for(slug: str | None) -> str | None:
    if not slug:
        return None
    return discover().get(slug.lower())


def repo_files_for(slug: str | None, query: str, max_files: int = 5,
                   max_chars_each: int = 1500) -> list[dict]:
    """Rank files in the project repo by query-term overlap in path+content and
    return the best snippets. Read-only. Skips huge/binary files."""
    root = repo_for(slug)
    if not root:
        return []
    base = Path(root)
    if not base.exists():
        return []
    terms = {t for t in re.findall(r"[A-Za-z0-9_]{3,}", (query or "").lower())}
    if not terms:
        return []
    scored: list[tuple[int, Path]] = []
    count = 0
    for p in base.rglob("*"):
        if count > 8000:
            break
        if p.is_dir():
            continue
        if any(part.lower() in _SKIP_DIRS for part in p.parts):
            continue
        if p.suffix.lower() not in _CODE_EXT:
            continue
        name_l = p.name.lower()
        if ".min." in name_l or name_l.endswith(".bundle.js") or "_bin_prod" in name_l:
            continue  # minified bundles are not readable source
        try:
            if p.stat().st_size > 200_000:
                continue  # data dumps / generated files
        except Exception:
            continue
        count += 1
        path_l = str(p).lower()
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        tl = text.lower()
        # Relevance = how many DISTINCT query terms the file covers (a file that
        # mentions reaper+heavy+subprocess+kill+idle beats one that repeats "the"
        # 2000×). Raw frequency is capped so big files can't dominate. Path hits
        # and source dirs (lib/scripts/src/app) weigh extra. Bruno 2026-07-01.
        path_hits = sum(1 for t in terms if t in path_l)
        present = sum(1 for t in terms if t in tl)
        # Stem match bridges natural words to snake_case symbols: "reaper" → the
        # 4-char stem "reap" hits `_reap_heavy`. Weak signal (weight 2).
        stem = sum(1 for t in terms if len(t) >= 5 and t[:4] in tl and t not in tl)
        if present == 0 and path_hits == 0 and stem == 0:
            continue
        total = sum(tl.count(t) for t in terms)
        src_bonus = 3 if re.search(r"[\\/](lib|scripts|src|app|core)[\\/]", path_l) else 0
        score = path_hits * 6 + present * 5 + stem * 2 + min(total, 12) + src_bonus
        scored.append((score, p))
    scored.sort(key=lambda x: x[0], reverse=True)
    out: list[dict] = []
    for score, p in scored[:max_files]:
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        snippet = _best_window(text, terms, max_chars_each)
        out.append({
            "path": str(p.relative_to(base)),
            "repo": base.name,
            "score": score,
            "snippet": snippet,
        })
    return out


def _best_window(text: str, terms: set[str], width: int) -> str:
    """Return the ~width-char window covering the most DISTINCT query terms (plus
    snake_case stems), so we inject the relevant function/params — not the file
    top. Distinct-term coverage beats raw frequency for landing on the right code."""
    if len(text) <= width:
        return text.strip()
    tl = text.lower()
    stems = {t[:4] for t in terms if len(t) >= 5}
    best_pos, best_score = 0, -1
    step = max(1, width // 3)
    for pos in range(0, len(text), step):
        window = tl[pos:pos + width]
        score = sum(2 for t in terms if t in window) + sum(1 for s in stems if s in window)
        if score > best_score:
            best_score, best_pos = score, pos
    start = max(0, best_pos - 80)  # a little lead-in for context
    return text[start:start + width].strip()
