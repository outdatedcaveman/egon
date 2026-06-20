"""Unified resource mirror — deduplicates references (Zotero, Paperpile) and files.

Performs cross-source deduplication (DOI, title, URL) and mirrors them to
Obsidian (under 050 - Mirrors/unified_resources) and Notion.
Injects semantic neighbor links computed from the Connect index.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from lib import semantic_index as si

ROOT = Path(__file__).resolve().parent.parent
VAULT = Path(r"C:\Users\bruno\Documents\Obsidian Vault")
MIRROR_DIR = VAULT / "050 - Mirrors" / "unified_resources"

_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe(name: str, maxlen: int = 80) -> str:
    s = _BAD.sub("_", str(name)).strip(". ")
    return (s[:maxlen] or "untitled").rstrip(". ")


def normalize_doi(doi: str) -> str:
    if not doi:
        return ""
    d = doi.strip().lower()
    if d.startswith("http://dx.doi.org/"):
        d = d[len("http://dx.doi.org/"):]
    elif d.startswith("https://doi.org/"):
        d = d[len("https://doi.org/"):]
    return d


def normalize_title(title: str) -> str:
    if not title:
        return ""
    t = title.strip().lower()
    t = re.sub(r"[^a-z0-9]", "", t)
    return t


def _file_digest(path: str) -> str:
    return hashlib.md5(path.encode("utf-8", "ignore")).hexdigest()


def _is_hydrated(file_item: dict) -> tuple[bool, Path]:
    digest = _file_digest(file_item["path"])
    extract_path = ROOT / "state" / "file_extracts" / f"{digest}.txt"
    return extract_path.exists(), extract_path


def deduplicate_resources() -> list[dict[str, Any]]:
    """Load Zotero, Paperpile, and Files and group them into deduplicated entities."""
    # 1. Load Zotero
    zot_items = []
    try:
        from lib.adapters import zotero_local
        zot_snap = zotero_local.snapshot()
        if zot_snap.get("status") == "ok":
            zot_items = zot_snap.get("items") or []
    except Exception as e:
        print(f"[unified_mirror] zotero load error: {e}", flush=True)

    # 2. Load Paperpile
    pp_items = []
    try:
        from lib.snapshot_store import latest_snapshot
        pp_snap = latest_snapshot("paperpile")
        if pp_snap:
            pp_items = pp_snap.get("items") or []
    except Exception as e:
        print(f"[unified_mirror] paperpile load error: {e}", flush=True)

    # 3. Load Files Index
    file_items = []
    files_jsonl = ROOT / "state" / "files_index.jsonl"
    if files_jsonl.exists():
        try:
            with files_jsonl.open(encoding="utf-8") as f:
                for line in f:
                    try:
                        file_items.append(json.loads(line))
                    except Exception:
                        continue
        except Exception as e:
            print(f"[unified_mirror] files index load error: {e}", flush=True)

    groups: list[dict[str, Any]] = []
    doi_to_group = {}
    title_to_group = {}
    url_to_group = {}

    def add_to_group(item: dict, source: str):
        doi = normalize_doi(item.get("doi") or "")
        title = item.get("title") or item.get("name") or ""
        norm_title = normalize_title(title)
        url = item.get("url") or ""

        group = None
        if doi and doi in doi_to_group:
            group = doi_to_group[doi]
        elif norm_title and len(norm_title) > 12 and norm_title in title_to_group:
            group = title_to_group[norm_title]
        elif url and url in url_to_group:
            group = url_to_group[url]

        if not group:
            group = {
                "title": title,
                "doi": doi,
                "url": url,
                "zotero_items": [],
                "paperpile_items": [],
                "files": [],
                "abstract": "",
                "year": "",
                "authors": []
            }
            groups.append(group)
            if doi:
                doi_to_group[doi] = group
            if norm_title and len(norm_title) > 12:
                title_to_group[norm_title] = group
            if url:
                url_to_group[url] = group

        # Update properties
        if not group["title"] and title:
            group["title"] = title
        if not group["doi"] and doi:
            group["doi"] = doi
            doi_to_group[doi] = group
        if not group["url"] and url:
            group["url"] = url
            url_to_group[url] = group

        # Extract abstract/metadata
        if item.get("abstract") and len(item["abstract"]) > len(group["abstract"]):
            group["abstract"] = item["abstract"]
        if item.get("year") and not group["year"]:
            group["year"] = str(item["year"])
        if item.get("added") and not group["year"] and len(str(item["added"])) >= 4:
            group["year"] = str(item["added"])[:4]
        if item.get("authors") and not group["authors"]:
            group["authors"] = item["authors"]
        elif item.get("creator") and not group["authors"]:
            group["authors"] = [item["creator"]]

        if source == "zotero":
            group["zotero_items"].append(item)
        elif source == "paperpile":
            group["paperpile_items"].append(item)
        elif source == "file":
            group["files"].append(item)

    # Process references first to form base groups
    for item in zot_items:
        add_to_group(item, "zotero")
    for item in pp_items:
        add_to_group(item, "paperpile")

    # Match files to groups
    for f_item in file_items:
        file_stem = Path(f_item["name"]).stem
        norm_stem = normalize_title(file_stem)
        
        # Substring / title matching to reference groups
        matched_group = None
        if norm_stem in title_to_group:
            matched_group = title_to_group[norm_stem]
        else:
            # Check substring matches
            for g in groups:
                norm_g = normalize_title(g["title"])
                if len(norm_g) > 12 and (norm_g in norm_stem or norm_stem in norm_g):
                    matched_group = g
                    break
        
        hydrated, _ = _is_hydrated(f_item)
        if matched_group:
            matched_group["files"].append(f_item)
        elif hydrated:
            # Hydrated files without matches become standalone unified entities
            add_to_group(f_item, "file")

    # Filter: Keep only groups that are Zotero, Paperpile, or have hydrated files
    curated_groups = []
    for g in groups:
        has_ref = len(g["zotero_items"]) > 0 or len(g["paperpile_items"]) > 0
        has_hydrated = any(_is_hydrated(f)[0] for f in g["files"])
        if has_ref or has_hydrated:
            curated_groups.append(g)

    return curated_groups


def _yaml_val(v) -> str:
    s = str(v).replace('"', "'").replace("\n", " ")[:300]
    return f'"{s}"'


def build_obsidian_unified_notes(groups: list[dict[str, Any]]) -> int:
    """Mirror deduplicated groups to Obsidian vault under unified_resources."""
    if not VAULT.is_dir():
        print(f"[unified_mirror] vault not found: {VAULT}", flush=True)
        return 0

    MIRROR_DIR.mkdir(parents=True, exist_ok=True)
    written = 0
    t0 = time.time()

    # Load semantic index to find neighbors
    si_ready = si.is_ready()

    for g in groups:
        title = g["title"]
        safe_name = _safe(title)
        
        # Unique key identifier
        key = ""
        if g["doi"]:
            key = f"doi:{g['doi']}"
        elif g["zotero_items"]:
            key = f"zot:{g['zotero_items'][0]['id']}"
        elif g["paperpile_items"]:
            key = f"pp:{g['paperpile_items'][0].get('id', '')}"
        elif g["files"]:
            key = f"file:{_file_digest(g['files'][0]['path'])}"
        else:
            key = f"title:{normalize_title(title)[:30]}"

        # Frontmatter
        fm = [
            "---",
            f"title: {_yaml_val(title)}",
            "source: unified_resource",
            f"key: {_yaml_val(key)}",
            f'mirrored_at: "{time.strftime("%Y-%m-%dT%H:%M:%S")}"'
        ]

        if g["doi"]:
            fm.append(f"doi: {_yaml_val(g['doi'])}")
        if g["year"]:
            fm.append(f"year: {_yaml_val(g['year'])}")
        
        # Provenance lists
        if g["zotero_items"]:
            fm.append(f"zotero_ids: [{', '.join(str(z['id']) for z in g['zotero_items'])}]")
        if g["paperpile_items"]:
            fm.append(f"paperpile_ids: [{', '.join(_yaml_val(p.get('id','')) for p in g['paperpile_items'])}]")
        if g["files"]:
            paths_str = ", ".join(_yaml_val(f["path"]) for f in g["files"])
            fm.append(f"file_paths: [{paths_str}]")

        if g["authors"]:
            fm.append(f"authors: [{', '.join(_yaml_val(a) for a in g['authors'])}]")

        # Semantic neighbors
        neighbors = []
        if si_ready:
            try:
                # Find semantic neighbors in connect index
                hits = si.search(title, top_k=7)
                for h in hits:
                    h_uid = h.get("uid", "")
                    # Filter out self
                    is_self = False
                    if g["doi"] and normalize_doi(h.get("doi") or "") == g["doi"]:
                        is_self = True
                    for z in g["zotero_items"]:
                        if f"zot:{z['id']}" in h_uid or f"memory:{z['id']}" in h_uid:
                            is_self = True
                    for f in g["files"]:
                        if _file_digest(f["path"]) in h_uid:
                            is_self = True
                    
                    if not is_self and len(neighbors) < 5:
                        neighbors.append(f"[[{_safe(h['title'])}]]")
            except Exception as e:
                print(f"[unified_mirror] semantic search neighbor error: {e}", flush=True)

        if neighbors:
            fm.append("semantic_neighbors:")
            for n in neighbors:
                fm.append(f"  - {n}")

        fm.append("tags: [mirror, unified_resource]")
        fm.append("---")
        fm.append("")

        body = []
        # Abstract section
        if g["abstract"]:
            body.append(f"## Abstract\n\n{g['abstract']}")

        # Hydrated content section
        for f in g["files"]:
            hydrated, ext_path = _is_hydrated(f)
            if hydrated:
                try:
                    content = ext_path.read_text(encoding="utf-8", errors="replace")[:12000]
                    body.append(f"## Content ({f['name']})\n\n{content}")
                except Exception:
                    pass

        note_text = "\n".join(fm) + "\n" + "\n\n".join(body) + "\n"
        try:
            (MIRROR_DIR / f"{safe_name}.md").write_text(note_text, encoding="utf-8")
            written += 1
        except Exception as e:
            print(f"[unified_mirror] write error for {safe_name}: {e}", flush=True)

    print(f"[unified_mirror] Mirrored {written} unified resources to Obsidian in {round(time.time() - t0, 1)}s", flush=True)
    return written


def mirror_to_notion_db(groups: list[dict[str, Any]], max_items: int = 500) -> dict:
    """Sync unified resources to a dedicated Notion Database."""
    from lib.notion_mirror import mirror_to_notion
    
    # Format unified groups to look like standard snapshot items for mirror_to_notion
    formatted_items = []
    for g in groups:
        # Build stable key
        key = ""
        if g["doi"]:
            key = f"doi:{g['doi']}"
        elif g["zotero_items"]:
            key = f"zot:{g['zotero_items'][0]['id']}"
        elif g["paperpile_items"]:
            key = f"pp:{g['paperpile_items'][0].get('id', '')}"
        elif g["files"]:
            key = f"file:{_file_digest(g['files'][0]['path'])}"
        else:
            key = f"title:{normalize_title(g['title'])[:30]}"

        detail = g["abstract"] or ""
        if g["files"]:
            detail += f" [Files: {', '.join(f['name'] for f in g['files'])}]"

        formatted_items.append({
            "_key": key,
            "title": g["title"],
            "url": g["url"] or (g["files"][0]["path"] if g["files"] else ""),
            "abstract": detail[:1000],
            "kind": "unified_resource",
            "year": g["year"] or "",
            "id": key
        })

    # Trigger mirror
    try:
        res = mirror_to_notion("unified_resources", {"items": formatted_items}, max_items=max_items)
        return res
    except Exception as e:
        return {"status": "error", "error": str(e)}


def run_unified_mirror() -> dict:
    """Execute deduplication, Obsidian mirroring, and Notion mirroring."""
    groups = deduplicate_resources()
    obsidian_written = build_obsidian_unified_notes(groups)
    
    notion_res = {}
    try:
        from lib import secrets
        # Only run Notion mirror if enabled in config
        if secrets.get("mirror.notion_enabled", False) and secrets.get("mirror.notion.unified_resources", False):
            notion_res = mirror_to_notion_db(groups)
    except Exception as e:
        notion_res = {"status": "error", "error": str(e)}

    return {
        "status": "ok",
        "groups_found": len(groups),
        "obsidian_written": obsidian_written,
        "notion_result": notion_res
    }


if __name__ == "__main__":
    print(run_unified_mirror())
