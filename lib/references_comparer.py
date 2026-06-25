"""References Comparer — backend comparison and syncing engine.

Groups references from Zotero, Paperpile, and Mouseion via DOIs and titles,
computes metadata completeness scores, and coordinates pushes/updates.
"""
from __future__ import annotations

import os
import re
import sys
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from lib import secrets
from lib.snapshot_store import latest_snapshot

# Mouseion local Flask URL
MOUSEION_FLASK = "http://127.0.0.1:7274"

TITLE_CLEAN_RX = re.compile(r"[^a-z0-9]")

def normalize_doi(doi: str) -> str:
    if not doi:
        return ""
    d = doi.strip().lower()
    if d.startswith("h"):
        if d.startswith("https://doi.org/"):
            return d[16:]
        if d.startswith("http://dx.doi.org/"):
            return d[18:]
    return d

def normalize_title(title: str) -> str:
    if not title:
        return ""
    return TITLE_CLEAN_RX.sub("", title.strip().lower())

def calculate_completeness(item: dict) -> int:
    """Compute completeness score (0-100) based on metadata richness."""
    score = 0
    if item.get("doi"):
        score += 25
    abstract = item.get("abstract") or ""
    if abstract:
        score += 15
        if len(abstract) > 100:
            score += 10
        if len(abstract) > 250:
            score += 10
    if item.get("authors") or item.get("creators"):
        score += 15
    if item.get("publication") or item.get("journal"):
        score += 15
    if item.get("url"):
        score += 10
    if item.get("year"):
        score += 10
    return score

def get_zotero_creds() -> Tuple[Optional[str], Optional[str]]:
    """Resolve Zotero User ID and API key from Egon config or panop_env.json."""
    uid = secrets.get("zotero.user_id")
    key = secrets.get("zotero.api_key")
    if uid and key:
        return str(uid), str(key)
    # Fallback to panop_env.json
    root = Path(__file__).resolve().parent.parent
    for p in (root / "panop_env.json", root.parent / "egon-local" / "config" / "connectors.env"):
        if p.exists():
            try:
                if p.suffix == ".json":
                    d = json.loads(p.read_text(encoding="utf-8-sig"))
                    if d.get("zotero_api_key") and d.get("zotero_user_id"):
                        return str(d["zotero_user_id"]), str(d["zotero_api_key"])
                else:
                    # env file
                    for line in p.read_text(encoding="utf-8").splitlines():
                        if "ZOTERO_USER_ID" in line:
                            uid = line.split("=")[1].strip().strip('"').strip("'")
                        elif "ZOTERO_API_KEY" in line:
                            key = line.split("=")[1].strip().strip('"').strip("'")
                    if uid and key:
                        return uid, key
            except Exception:
                continue
    return None, None

def get_mouseion_api_key() -> Optional[str]:
    """Retrieve Mouseion API key from key file next to refs.db."""
    from lib.adapters import mouseion
    db_path = mouseion._db_path()
    if db_path:
        key_path = db_path.with_suffix(".key")
        if key_path.exists():
            try:
                return key_path.read_text(encoding="utf-8").strip()
            except Exception:
                pass
    return None

def build_comparer_index(limit: int = 10000, progress_callback=None) -> Tuple[List[dict], dict]:
    """Load items from Zotero, Paperpile, and Mouseion and deduplicate into groups.
    
    Default limit is 10,000 per source (not 350,000) to prevent memory exhaustion.
    Bruno 2026-06-24: previous 350k default caused PC freeze when consolidation
    tried to process all items at once.
    """
    t0 = time.time()
    
    # 1. Fetch Zotero (skip tags to speed up load time)
    if progress_callback:
        progress_callback(0, 4, "Loading Zotero items from local database...")
    zotero_items = []
    try:
        from lib.adapters import zotero_local
        zotero_items = zotero_local.items(limit, include_tags=False)
    except Exception:
        pass
    if not zotero_items:
        snap = latest_snapshot("zotero_web")
        if snap:
            zotero_items = snap.get("items") or []

    # 2. Fetch Paperpile
    if progress_callback:
        progress_callback(1, 4, "Loading Paperpile items...")
    paperpile_items = []
    try:
        from lib.adapters import paperpile
        paperpile_items = paperpile.items(limit)
    except Exception:
        pass

    # 3. Fetch Mouseion (skip slow unindexed sorting)
    if progress_callback:
        progress_callback(2, 4, "Loading Mouseion items...")
    mouseion_items = []
    try:
        from lib.adapters import mouseion
        mouseion_items = mouseion.items(limit, sort_by_date=False)
    except Exception:
        pass

    if progress_callback:
        progress_callback(3, 4, "Deduplicating and grouping references...")
    groups: List[dict] = []
    doi_to_group = {}
    title_to_group = {}

    def add_to_group(item: dict, source: str):
        raw_doi = item.get("doi")
        doi = normalize_doi(raw_doi) if raw_doi else ""
        
        title = item.get("title") or ""
        norm_title = TITLE_CLEAN_RX.sub("", title.strip().lower())
        
        group = None
        if doi and doi in doi_to_group:
            group = doi_to_group[doi]
        elif norm_title and len(norm_title) > 12 and norm_title in title_to_group:
            group = title_to_group[norm_title]

        if not group:
            group = {
                "title": title,
                "authors": item.get("authors") or "",
                "year": item.get("year") or "",
                "doi": doi,
                "url": item.get("url") or "",
                "publication": item.get("publication") or "",
                "abstract": item.get("abstract") or "",
                "zotero_item": None,
                "paperpile_item": None,
                "mouseion_item": None,
                "score_zotero": 0,
                "score_paperpile": 0,
                "score_mouseion": 0,
                "best_source": "",
                "best_score": 0,
            }
            groups.append(group)
            if doi:
                doi_to_group[doi] = group
            if norm_title and len(norm_title) > 12:
                title_to_group[norm_title] = group

        # Update core fields if richer
        if not group["title"] and title:
            group["title"] = title
        if not group["doi"] and doi:
            group["doi"] = doi
            doi_to_group[doi] = group
        if not group["url"] and item.get("url"):
            group["url"] = item["url"]
        if not group["year"] and item.get("year"):
            group["year"] = item["year"]
        if not group["authors"] and item.get("authors"):
            group["authors"] = item["authors"]
        if not group["publication"] and item.get("publication"):
            group["publication"] = item["publication"]
        
        abstract = item.get("abstract") or ""
        if len(abstract) > len(group["abstract"]):
            group["abstract"] = abstract

        # Fast inlined calculate_completeness
        score = 0
        if doi:
            score += 25
        if abstract:
            score += 15
            l_abs = len(abstract)
            if l_abs > 100:
                score += 10
            if l_abs > 250:
                score += 10
        if item.get("authors") or item.get("creators"):
            score += 15
        if item.get("publication") or item.get("journal"):
            score += 15
        if item.get("url"):
            score += 10
        if item.get("year"):
            score += 10

        if source == "zotero":
            group["zotero_item"] = item
            group["score_zotero"] = score
        elif source == "paperpile":
            group["paperpile_item"] = item
            group["score_paperpile"] = score
        elif source == "mouseion":
            group["mouseion_item"] = item
            group["score_mouseion"] = score

        # Update best source details
        best_source = "Zotero"
        best_score = group["score_zotero"]
        if group["score_paperpile"] > best_score:
            best_source = "Paperpile"
            best_score = group["score_paperpile"]
        if group["score_mouseion"] > best_score:
            best_source = "Mouseion"
            best_score = group["score_mouseion"]
        group["best_source"] = best_source
        group["best_score"] = best_score
        group["best_score"] = best_score

    # Process Zotero, Paperpile, Mouseion items
    for it in zotero_items:
        add_to_group(it, "zotero")
    for it in paperpile_items:
        add_to_group(it, "paperpile")
    for it in mouseion_items:
        add_to_group(it, "mouseion")

    # Pre-calculate search_blob to avoid CPU-bound formatting/lower() on the GUI thread
    for g in groups:
        g_title = g.get("title") or ""
        g_authors = g.get("authors") or ""
        g_doi = g.get("doi") or ""
        g_year = g.get("year") or ""
        g["search_blob"] = f"{g_title} {g_authors} {g_doi} {g_year}".lower()

    # Sort groups by title alphabetically
    groups.sort(key=lambda g: (g["title"] or "").lower())

    if progress_callback:
        progress_callback(4, 4, "Grouping complete.")

    stats = {
        "total_unique": len(groups),
        "zotero_count": len(zotero_items),
        "paperpile_count": len(paperpile_items),
        "mouseion_count": len(mouseion_items),
        "perfect_match": sum(1 for g in groups if g["zotero_item"] and g["paperpile_item"] and g["mouseion_item"]),
        "duration": round(time.time() - t0, 3)
    }

    return groups, stats

def to_ris_string(items: List[dict]) -> str:
    """Format dictionary entries as a RIS format string."""
    lines = []
    from lib.adapters import mouseion
    for it in items:
        lines.append("TY  - JOUR")
        if it.get("title"):
            lines.append(f"TI  - {it['title']}")
        authors = it.get("authors") or ""
        if authors.startswith("["):
            authors = mouseion._clean_authors(authors)
        for a in authors.split(","):
            a = a.strip()
            if a:
                lines.append(f"AU  - {a}")
        if it.get("year"):
            lines.append(f"PY  - {it['year']}")
        if it.get("publication"):
            lines.append(f"JO  - {it['publication']}")
        if it.get("doi"):
            lines.append(f"DO  - {it['doi']}")
        if it.get("url"):
            lines.append(f"UR  - {it['url']}")
        if it.get("abstract"):
            lines.append(f"AB  - {it['abstract']}")
        lines.append("ER  - ")
        lines.append("")
    return "\n".join(lines)

def to_zotero_payload(ref: dict) -> dict:
    """Format a standard ref dictionary to Zotero API format."""
    creators = []
    authors = ref.get("authors") or ""
    if authors.startswith("["):
        from lib.adapters import mouseion
        authors = mouseion._clean_authors(authors)
    for a in authors.split(","):
        a = a.strip()
        if not a:
            continue
        parts = a.split(" ")
        if len(parts) >= 2:
            lastName = parts[-1]
            firstName = " ".join(parts[:-1])
        else:
            lastName = a
            firstName = ""
        creators.append({
            "creatorType": "author",
            "firstName": firstName,
            "lastName": lastName
        })
    
    payload = {
        "itemType": "journalArticle",
        "title": ref.get("title") or "(untitled)",
        "creators": creators,
        "date": str(ref.get("year") or ""),
        "url": ref.get("url") or "",
        "DOI": ref.get("doi") or "",
        "abstractNote": ref.get("abstract") or "",
        "publicationTitle": ref.get("publication") or ""
    }
    return payload

def push_to_zotero(items: List[dict], progress_callback=None) -> Tuple[int, int]:
    """Push items to Zotero Web API in batches of 50."""
    uid, key = get_zotero_creds()
    if not uid or not key:
        raise RuntimeError("Zotero credentials not found in egon-config.json or panop_env.json")
    
    url = f"https://api.zotero.org/users/{uid}/items"
    headers = {
        "Zotero-API-Key": key,
        "Zotero-API-Version": "3",
        "Content-Type": "application/json"
    }

    success = 0
    failed = 0
    BATCH = 50
    for i in range(0, len(items), BATCH):
        if progress_callback:
            progress_callback(i, len(items), f"Pushing items {i+1} to {min(i+BATCH, len(items))} of {len(items)} to Zotero...")
        chunk = items[i:i+BATCH]
        payload = [to_zotero_payload(it) for it in chunk]
        try:
            r = httpx.post(url, headers=headers, json=payload, timeout=30)
            if r.status_code in (200, 201):
                body = r.json()
                success += len(body.get("successful") or body.get("success", {}))
                failed += len(body.get("failed") or {})
            else:
                failed += len(chunk)
        except Exception:
            failed += len(chunk)
        time.sleep(0.3)
    if progress_callback:
        progress_callback(len(items), len(items), "Zotero push complete.")
    return success, failed

def push_to_mouseion(items: List[dict], progress_callback=None) -> Tuple[int, int]:
    """Push items to Mouseion Flask API (fallback: direct SQLite write)."""
    if progress_callback:
        progress_callback(0, len(items), "Preparing push to Mouseion...")
    ris_content = to_ris_string(items)
    
    # 1. Try Flask API first
    api_key = get_mouseion_api_key()
    headers = {}
    if api_key:
        headers["X-API-Key"] = api_key
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        if progress_callback:
            progress_callback(0, len(items), "Uploading references to Mouseion API...")
        files = {"file": ("push.ris", ris_content, "application/x-research-info-systems")}
        data = {"enrich": "false"}
        r = httpx.post(f"{MOUSEION_FLASK}/api/import", headers=headers, files=files, data=data, timeout=30)
        if r.status_code == 200:
            if progress_callback:
                progress_callback(len(items), len(items), "Mouseion API upload successful.")
            return len(items), 0
    except Exception:
        pass

    # 2. Fallback to direct SQLite write via Mouseion DB model
    try:
        from lib.adapters import mouseion
        db_path = mouseion._db_path()
        if not db_path or not db_path.exists():
            return 0, len(items)

        # Import DB module dynamically by adding zoterpile-main path to sys.path
        mouseion_path = secrets.get("apps_cache.mouseion.install_path") or "C:\\Users\\bruno\\Desktop\\mnt\\outputs\\zoterpile-main"
        src_path = str(Path(mouseion_path) / "src")
        if src_path not in sys.path:
            sys.path.append(src_path)

        from mouseion.db import RefDatabase
        from mouseion.models import Reference, Author, RefType

        success = 0
        if progress_callback:
            progress_callback(0, len(items), "Writing entries directly to Mouseion SQLite...")
        with RefDatabase(str(db_path)) as db:
            for idx, it in enumerate(items):
                ref = Reference(
                    title=it.get("title") or "",
                    doi=it.get("doi") or None,
                    url=it.get("url") or None,
                    year=int(it.get("year")) if it.get("year") and str(it.get("year")).isdigit() else None,
                    abstract=it.get("abstract") or None,
                    journal=it.get("publication") or None,
                    ref_type=RefType.JOURNAL
                )
                authors = it.get("authors") or ""
                if authors.startswith("["):
                    import json as _j
                    try:
                        creators = _j.loads(authors)
                        for c in creators:
                            fam = c.get("family") or c.get("last") or c.get("name") or ""
                            giv = c.get("given") or c.get("first") or ""
                            if fam or giv:
                                ref.authors.append(Author(last=fam, first=giv))
                    except Exception:
                        pass
                else:
                    for a in authors.split(","):
                        a = a.strip()
                        if a:
                            ref.authors.append(Author.from_bibtex_str(a))
                try:
                    db.upsert(ref)
                    success += 1
                except Exception:
                    pass
                if progress_callback and idx % 100 == 0:
                    progress_callback(idx, len(items), f"Writing directly to Mouseion SQLite ({idx}/{len(items)})...")
        if progress_callback:
            progress_callback(len(items), len(items), "Mouseion SQLite direct write complete.")
        return success, len(items) - success
    except Exception:
        return 0, len(items)
    except Exception:
        return 0, len(items)

def push_to_paperpile(items: List[dict]) -> str:
    """Save items to a RIS export file in the user's Downloads folder."""
    downloads = Path.home() / "Downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    out_file = downloads / "Egon_to_Paperpile.ris"
    
    ris_content = to_ris_string(items)
    out_file.write_text(ris_content, encoding="utf-8")
    return str(out_file)

def merge_fields(z_item: Optional[dict], p_item: Optional[dict], m_item: Optional[dict]) -> dict:
    """Helper to merge fields across multiple items, picking the richest representation."""
    sources = [item for item in (z_item, p_item, m_item) if item]
    if not sources:
        return {}

    def best_str(field_name: str) -> str:
        candidates = []
        for item in sources:
            val = item.get(field_name)
            if val and isinstance(val, str):
                candidates.append(val.strip())
        if not candidates:
            return ""
        filtered = [c for c in candidates if c.lower() not in ("(untitled)", "untitled", "n/a", "none")]
        if filtered:
            return max(filtered, key=len)
        return max(candidates, key=len)

    def best_year() -> str:
        candidates = []
        for item in sources:
            val = item.get("year")
            if val:
                val_str = str(val).strip()
                if val_str:
                    candidates.append(val_str)
        four_digit = [c for c in candidates if len(c) == 4 and c.isdigit()]
        if four_digit:
            return four_digit[0]
        if candidates:
            return candidates[0]
        return ""

    def best_doi() -> str:
        candidates = []
        for item in sources:
            val = item.get("doi")
            if val:
                val_str = normalize_doi(val)
                if val_str:
                    candidates.append(val_str)
        if candidates:
            return max(candidates, key=len)
        return ""

    def best_url() -> str:
        candidates = []
        for item in sources:
            val = item.get("url")
            if val:
                val_str = val.strip()
                if val_str:
                    candidates.append(val_str)
        if candidates:
            return max(candidates, key=len)
        return ""

    return {
        "title": best_str("title"),
        "authors": best_str("authors"),
        "year": best_year(),
        "doi": best_doi(),
        "url": best_url(),
        "publication": best_str("publication"),
        "abstract": best_str("abstract"),
    }

def needs_update(client_item: dict, merged_item: dict) -> bool:
    """Determine if a client's metadata item needs update relative to the merged representation."""
    for field in ("title", "authors", "year", "doi", "url", "publication", "abstract"):
        c_val = (client_item.get(field) or "").strip()
        m_val = (merged_item.get(field) or "").strip()
        if not m_val:
            continue
        if not c_val and m_val:
            return True
        if field in ("title", "authors", "publication", "abstract"):
            if len(m_val) > len(c_val):
                return True
        elif field == "doi":
            if normalize_doi(c_val) != normalize_doi(m_val):
                return True
        elif field == "url":
            if c_val != m_val and len(m_val) > len(c_val):
                return True
        elif field == "year":
            if c_val != m_val and len(m_val) == 4 and m_val.isdigit():
                return True
    return False

def run_consolidation(items: List[dict], merge_sources: bool = True, target_client: Optional[str] = None, progress_callback=None) -> dict:
    """Update metadata to matching richer versions in Zotero and Mouseion."""
    uid, zot_key = get_zotero_creds()
    m_key = get_mouseion_api_key()

    zotero_updates = []
    mouseion_updates = []
    paperpile_updates = []

    for it in items:
        if merge_sources:
            rich_payload = merge_fields(it["zotero_item"], it["paperpile_item"], it["mouseion_item"])
        else:
            best_source = it.get("best_source")
            if not best_source:
                continue
            rich_payload = None
            if best_source == "Zotero":
                rich_payload = it["zotero_item"]
            elif best_source == "Paperpile":
                rich_payload = it["paperpile_item"]
            elif best_source == "Mouseion":
                rich_payload = it["mouseion_item"]

        if not rich_payload:
            continue

        # 1. Zotero Update Queue
        z_item = it["zotero_item"]
        if z_item and (target_client is None or target_client == "zotero"):
            should_update = False
            if merge_sources:
                should_update = needs_update(z_item, rich_payload)
            else:
                should_update = (best_source != "Zotero" and it["best_score"] > it["score_zotero"])
                
            if should_update:
                z_key = z_item.get("id") or z_item.get("key")
                if z_key:
                    zotero_updates.append({
                        "key": z_key,
                        "title": rich_payload.get("title") or z_item.get("title"),
                        "abstract": rich_payload.get("abstract") or z_item.get("abstract"),
                        "doi": rich_payload.get("doi") or z_item.get("doi"),
                        "url": rich_payload.get("url") or z_item.get("url"),
                        "year": rich_payload.get("year") or z_item.get("year"),
                        "authors": rich_payload.get("authors") or z_item.get("authors") or z_item.get("creators"),
                        "publication": rich_payload.get("publication") or z_item.get("publication")
                    })

        # 2. Mouseion Update Queue
        m_item = it["mouseion_item"]
        if m_item and (target_client is None or target_client == "mouseion"):
            should_update = False
            if merge_sources:
                should_update = needs_update(m_item, rich_payload)
            else:
                should_update = (best_source != "Mouseion" and it["best_score"] > it["score_mouseion"])
                
            if should_update:
                m_id = m_item.get("id")
                if m_id:
                    mouseion_updates.append({
                        "id": m_id,
                        "title": rich_payload.get("title") or m_item.get("title"),
                        "abstract": rich_payload.get("abstract") or m_item.get("abstract"),
                        "doi": rich_payload.get("doi") or m_item.get("doi"),
                        "url": rich_payload.get("url") or m_item.get("url"),
                        "year": rich_payload.get("year") or m_item.get("year"),
                        "authors": rich_payload.get("authors") or m_item.get("authors") or m_item.get("creators"),
                        "publication": rich_payload.get("publication") or m_item.get("publication")
                    })

        # 3. Paperpile Update Queue (for RIS download)
        p_item = it["paperpile_item"]
        if p_item and (target_client is None or target_client == "paperpile"):
            should_update = False
            if merge_sources:
                should_update = needs_update(p_item, rich_payload)
            else:
                should_update = (best_source != "Paperpile" and it["best_score"] > it["score_paperpile"])
                
            if should_update:
                paperpile_updates.append(rich_payload)

    # Perform updates
    zot_success = zot_fail = 0
    if zotero_updates and uid and zot_key:
        # Fetch current versions first via Web API (required for patch)
        base = f"https://api.zotero.org/users/{uid}"
        headers = {"Zotero-API-Key": zot_key, "Zotero-API-Version": "3"}
        
        # Batch version lookups
        keys = [x["key"] for x in zotero_updates]
        ver = {}
        for i in range(0, len(keys), 50):
            chunk = keys[i:i+50]
            if progress_callback:
                progress_callback(i, len(keys), f"Looking up Zotero item versions ({i}/{len(keys)})...")
            try:
                r = httpx.get(f"{base}/items?itemKey={','.join(chunk)}&limit=50", headers=headers, timeout=20)
                if r.status_code == 200:
                    for obj in r.json():
                        ver[obj["key"]] = obj["version"]
            except Exception:
                pass

        # Update Zotero
        for i in range(0, len(zotero_updates), 50):
            chunk = [x for x in zotero_updates[i:i+50] if x["key"] in ver]
            if progress_callback:
                progress_callback(i, len(zotero_updates), f"Updating Zotero items ({i}/{len(zotero_updates)})...")
            payload = []
            for x in chunk:
                # payload must match Zotero's schema
                z_payload = to_zotero_payload(x)
                z_payload["key"] = x["key"]
                z_payload["version"] = ver[x["key"]]
                payload.append(z_payload)
            try:
                r = httpx.post(f"{base}/items", headers={**headers, "Content-Type": "application/json"},
                               json=payload, timeout=30)
                if r.status_code in (200, 201):
                    body = r.json()
                    zot_success += len(body.get("successful") or body.get("success", {}))
                    zot_fail += len(body.get("failed") or {})
                else:
                    zot_fail += len(chunk)
            except Exception:
                zot_fail += len(chunk)
            time.sleep(0.3)

    m_success = m_fail = 0
    if mouseion_updates:
        # Try direct SQLite write first as it is local and 100x faster (done in a single transaction)
        db_written = False
        try:
            from lib.adapters import mouseion
            db_path = mouseion._db_path()
            if db_path and db_path.exists():
                mouseion_path = secrets.get("apps_cache.mouseion.install_path") or "C:\\Users\\bruno\\Desktop\\mnt\\outputs\\zoterpile-main"
                src_path = str(Path(mouseion_path) / "src")
                if src_path not in sys.path:
                    sys.path.append(src_path)

                from mouseion.db import RefDatabase
                
                if progress_callback:
                    progress_callback(0, len(mouseion_updates), "Updating Mouseion database directly...")
                
                BATCH_SIZE = 100
                for chunk_idx in range(0, len(mouseion_updates), BATCH_SIZE):
                    chunk = mouseion_updates[chunk_idx:chunk_idx+BATCH_SIZE]
                    with RefDatabase(str(db_path)) as db:
                        for idx, x in enumerate(chunk):
                            try:
                                # Update fields
                                authors_str = x["authors"] or ""
                                if isinstance(authors_str, list):
                                    import json as _j
                                    authors_json = _j.dumps(authors_str)
                                elif authors_str.startswith("["):
                                    authors_json = authors_str
                                else:
                                    creators = []
                                    for a in authors_str.split(","):
                                        a = a.strip()
                                        if not a: continue
                                        parts = a.split(" ")
                                        if len(parts) >= 2:
                                            creators.append({"family": parts[-1], "given": " ".join(parts[:-1])})
                                        else:
                                            creators.append({"family": a, "given": ""})
                                    import json as _j
                                    authors_json = _j.dumps(creators)

                                db.update_ref_fields(
                                    x["id"],
                                    title=x["title"],
                                    abstract=x["abstract"],
                                    doi=x["doi"],
                                    url=x["url"],
                                    year=int(x["year"]) if str(x["year"]).isdigit() else None,
                                    authors_json=authors_json,
                                    journal=x["publication"]
                                )
                                m_success += 1
                            except Exception:
                                m_fail += 1
                    
                    if progress_callback:
                        progress_callback(chunk_idx + len(chunk), len(mouseion_updates), f"Writing to Mouseion SQLite ({chunk_idx + len(chunk)}/{len(mouseion_updates)})...")
                    
                    # Yield DB write lock and CPU slice to other threads/processes
                    time.sleep(0.1)
                            
                db_written = True
                if progress_callback:
                    progress_callback(len(mouseion_updates), len(mouseion_updates), "Mouseion SQLite update complete.")
        except Exception as e:
            pass

        if not db_written:
            # Fallback to HTTP PATCH
            headers = {}
            if m_key:
                headers["X-API-Key"] = m_key
                headers["Authorization"] = f"Bearer {m_key}"
            
            # Check if Flask is responsive first before looping to avoid timeouts
            flask_online = False
            try:
                r = httpx.get(f"{MOUSEION_FLASK}/api/status", headers=headers, timeout=2)
                if r.status_code == 200:
                    flask_online = True
            except Exception:
                pass
                
            if flask_online:
                BATCH_DELAY = 0.2  # seconds between batches of 10
                for idx, x in enumerate(mouseion_updates):
                    if progress_callback and idx % 10 == 0:
                        progress_callback(idx, len(mouseion_updates), f"Sending Mouseion HTTP PATCH ({idx}/{len(mouseion_updates)})...")
                    try:
                        creators = []
                        authors_str = x["authors"] or ""
                        if authors_str.startswith("["):
                            try:
                                import json as _j
                                creators = _j.loads(authors_str)
                            except Exception:
                                pass
                        else:
                            for a in authors_str.split(","):
                                a = a.strip()
                                if not a: continue
                                parts = a.split(" ")
                                if len(parts) >= 2:
                                    creators.append({"family": parts[-1], "given": " ".join(parts[:-1])})
                                else:
                                    creators.append({"family": a, "given": ""})
                        
                        payload = {
                            "title": x["title"],
                            "abstract": x["abstract"],
                            "doi": x["doi"],
                            "url": x["url"],
                            "year": int(x["year"]) if str(x["year"]).isdigit() else None,
                            "authors": creators,
                            "journal": x["publication"]
                        }
                        r = httpx.patch(f"{MOUSEION_FLASK}/api/refs/{x['id']}", headers=headers, json=payload, timeout=5)
                        if r.status_code == 200:
                            m_success += 1
                        else:
                            m_fail += 1
                    except Exception:
                        m_fail += 1
                    # Rate limit: sleep every 10 items to avoid CPU/network saturation
                    # Bruno 2026-06-24: previous code had ZERO delay, causing PC freeze
                    if (idx + 1) % 10 == 0:
                        time.sleep(BATCH_DELAY)
            else:
                m_fail += len(mouseion_updates)

    ris_file = ""
    if paperpile_updates:
        ris_file = push_to_paperpile(paperpile_updates)

    return {
        "status": "ok",
        "zotero": {"success": zot_success, "fail": zot_fail, "total_attempted": len(zotero_updates)},
        "mouseion": {"success": m_success, "fail": m_fail, "total_attempted": len(mouseion_updates)},
        "paperpile": {"ris_file": ris_file, "total_exported": len(paperpile_updates)}
    }
