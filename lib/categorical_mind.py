"""Categorical Mind — Applied Category Theory (ACT) engine.

Allows formal representation of concepts as Categories, relationships as Morphisms,
and structural analogies as Functors. Integrates with state/mind.db.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent
_DB_PATH = ROOT / "state" / "mind.db"


class Category:
    def __init__(self, name: str):
        self.name = name.strip()
        self.objects: set[str] = set()
        # Morphisms: (dom, codom) -> set of relationship labels
        self.morphisms: dict[tuple[str, str], set[str]] = {}

    def add_object(self, obj: str) -> None:
        self.objects.add(obj.strip())

    def add_morphism(self, dom: str, codom: str, label: str) -> None:
        dom = dom.strip()
        codom = codom.strip()
        label = label.strip()
        self.objects.add(dom)
        self.objects.add(codom)
        self.morphisms.setdefault((dom, codom), set()).add(label)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "objects": sorted(list(self.objects)),
            "morphisms": [
                {"dom": dom, "codom": codom, "labels": sorted(list(labels))}
                for (dom, codom), labels in self.morphisms.items()
            ]
        }


class Functor:
    def __init__(self, dom: Category, codom: Category):
        self.dom = dom
        self.codom = codom
        self.obj_map: dict[str, str] = {}
        # Morphism map: (dom_obj, codom_obj, label) -> label in codom category
        self.mor_map: dict[tuple[str, str, str], str] = {}

    def is_valid(self) -> bool:
        """Validate functor preservation conditions:
        1. All domain objects map to codomain objects.
        2. For every morphism f: A -> B in dom, there exists a mapped morphism
           F(f): F(A) -> F(B) in codom.
        """
        # 1. Object mapping validation
        for obj in self.dom.objects:
            if obj not in self.obj_map or self.obj_map[obj] not in self.codom.objects:
                return False

        # 2. Morphism mapping validation (preserves composition/boundaries)
        for (a, b), labels in self.dom.morphisms.items():
            fa = self.obj_map[a]
            fb = self.obj_map[b]
            for label in labels:
                mapped_label = self.mor_map.get((a, b, label))
                if not mapped_label:
                    return False
                # The mapped morphism must exist between F(A) and F(B)
                codom_labels = self.codom.morphisms.get((fa, fb))
                if not codom_labels or mapped_label not in codom_labels:
                    return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "dom": self.dom.name,
            "codom": self.codom.name,
            "obj_map": self.obj_map,
            "mor_map": {
                f"{dom_obj}->{codom_obj}:{lbl}": mapped_lbl
                for (dom_obj, codom_obj, lbl), mapped_lbl in self.mor_map.items()
            }
        }

    def summary(self) -> str:
        lines = [f"Functor (Analogy) from '{self.dom.name}' to '{self.codom.name}':"]
        for k, v in sorted(self.obj_map.items()):
            lines.append(f"  ● Object: {k} ↦ {v}")
        for (d, c, l), ml in sorted(self.mor_map.items()):
            lines.append(f"  ⚡ Morphism: ({d} -{l}→ {c}) ↦ ({self.obj_map[d]} -{ml}→ {self.obj_map[c]})")
        return "\n".join(lines)


# ── Parser ─────────────────────────────────────────────────────────────────

def parse_categories_from_markdown(text: str) -> list[Category]:
    """Parse categories defined in markdown files using block syntax:
    ```category
    name: CategoryName
    objects: ObjA, ObjB, ObjC
    morphisms:
      ObjA -> ObjB: morphLabel1
      ObjB -> ObjC: morphLabel2
    ```
    """
    categories = []
    # Match markdown codeblocks with language 'category'
    pattern = r"```category\s*([\s\S]*?)```"
    for block in re.findall(pattern, text):
        lines = block.splitlines()
        cat_name = "Unnamed"
        objects_list: list[str] = []
        morphisms_lines: list[str] = []
        in_morphisms = False

        for line in lines:
            line_str = line.strip()
            if not line_str:
                continue
            if line_str.lower().startswith("name:"):
                cat_name = line_str.split(":", 1)[1].strip()
            elif line_str.lower().startswith("objects:"):
                objs = line_str.split(":", 1)[1].split(",")
                objects_list = [o.strip() for o in objs if o.strip()]
            elif line_str.lower().startswith("morphisms:"):
                in_morphisms = True
            elif in_morphisms:
                morphisms_lines.append(line_str)

        cat = Category(cat_name)
        for obj in objects_list:
            cat.add_object(obj)

        for mor in morphisms_lines:
            # Format: DomObj -> CodomObj: label
            m = re.match(r"([A-Za-z0-9_.-]+)\s*->\s*([A-Za-z0-9_.-]+)\s*:\s*([A-Za-z0-9_.-]+)", mor)
            if m:
                cat.add_morphism(m.group(1), m.group(2), m.group(3))
        
        if cat.objects:
            categories.append(cat)
            
    return categories


# ── Functor Search Engine ──────────────────────────────────────────────────

def find_functors(dom: Category, codom: Category) -> list[Functor]:
    """Perform a backtracking search to find structure-preserving Functors
    between the domain and codomain categories.
    """
    if not dom.objects or not codom.objects:
        return []

    dom_objs = sorted(list(dom.objects))
    codom_objs = list(codom.objects)
    
    discovered: list[Functor] = []
    
    # Backtracking state
    obj_mapping: dict[str, str] = {}
    
    def search_objects(idx: int) -> None:
        if idx >= len(dom_objs):
            # Object mapping completed. Now attempt to map morphisms.
            f = Functor(dom, codom)
            f.obj_map = obj_mapping.copy()
            if map_morphisms(f):
                discovered.append(f)
            return

        dom_obj = dom_objs[idx]
        for codom_obj in codom_objs:
            obj_mapping[dom_obj] = codom_obj
            search_objects(idx + 1)
            del obj_mapping[dom_obj]

    def map_morphisms(f: Functor) -> bool:
        # For every domain morphism, search for matching codomain morphism
        # between the mapped objects.
        for (a, b), labels in dom.morphisms.items():
            fa = f.obj_map[a]
            fb = f.obj_map[b]
            codom_labels = codom.morphisms.get((fa, fb))
            
            if not codom_labels:
                return False  # No morphisms exist between F(A) and F(B)
                
            for label in labels:
                # Find a match. To prevent combinatorial explosion,
                # we match labels that share the same name if present,
                # or match any available morphism if labels are generic.
                matched = False
                # Try exact name match first
                if label in codom_labels:
                    f.mor_map[(a, b, label)] = label
                    matched = True
                else:
                    # Match any (take the first available)
                    for cl in codom_labels:
                        f.mor_map[(a, b, label)] = cl
                        matched = True
                        break
                if not matched:
                    return False
        return f.is_valid()

    search_objects(0)
    return discovered


# ── DB & File Ingestion ────────────────────────────────────────────────────

def scan_and_reconcile_categories() -> dict[str, Any]:
    """Scan all project files and brain notes for category definitions,
    find functors between them, and register strategy notes in the database.
    """
    categories: list[Category] = []
    
    # 1. Scan Antigravity brain sessions
    brain_dir = Path("C:/Users/bruno/.gemini/antigravity/brain")
    if brain_dir.exists():
        for path in brain_dir.rglob("*.md"):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
                categories.extend(parse_categories_from_markdown(text))
            except Exception: pass

    # 2. Scan Egon root markdown files, state/, and docs/ recursively
    scan_paths = list(ROOT.glob("*.md"))
    state_dir = ROOT / "state"
    if state_dir.exists():
        scan_paths.extend(state_dir.rglob("*.md"))
    docs_dir = ROOT / "docs"
    if docs_dir.exists():
        scan_paths.extend(docs_dir.rglob("*.md"))

    for path in scan_paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            categories.extend(parse_categories_from_markdown(text))
        except Exception: pass

    # Deduplicate categories by name
    unique_cats: dict[str, Category] = {}
    for c in categories:
        unique_cats[c.name] = c
        
    cat_list = list(unique_cats.values())
    functors_count = 0
    functor_summaries = []

    # 3. Search for functors between distinct categories
    if len(cat_list) >= 2:
        for i in range(len(cat_list)):
            for j in range(len(cat_list)):
                if i == j:
                    continue
                dom = cat_list[i]
                codom = cat_list[j]
                
                found = find_functors(dom, codom)
                for f in found:
                    functors_count += 1
                    functor_summaries.append(f.summary())
                    
                    # Store in SQLite database as kind='strategy'
                    _save_functor_to_db(f)

    return {
        "status": "ok",
        "categories": [c.to_dict() for c in cat_list],
        "functors_discovered_count": functors_count,
        "functors": functor_summaries
    }


def _save_functor_to_db(f: Functor) -> None:
    if not _DB_PATH.exists():
        return
    try:
        conn = sqlite3.connect(_DB_PATH, timeout=5)
        now = int(time.time())
        content = f"[ANALOGY] Found Category Functor from '{f.dom.name}' to '{f.codom.name}':\n{f.summary()}"
        tags = f"introspection,strategy,categorical_mind,analogy,{f.dom.name.lower()},{f.codom.name.lower()}"
        
        # Check if already exists to avoid cluttering
        cursor = conn.cursor()
        row = cursor.execute(
            "SELECT id FROM memory WHERE kind='strategy' AND content LIKE ?",
            (f"%Functor from '{f.dom.name}' to '{f.codom.name}'%",)).fetchone()
        
        if row:
            cursor.execute(
                "UPDATE memory SET content=?, updated_at=? WHERE id=?",
                (content, now, row[0])
            )
        else:
            cursor.execute(
                """INSERT INTO memory (kind, content, tags, created_at, updated_at)
                   VALUES ('strategy', ?, ?, ?, ?)""",
                (content, tags, now, now)
            )
        conn.commit()
        conn.close()
    except Exception:
        pass


if __name__ == "__main__":
    # Test block: print out category results
    res = scan_and_reconcile_categories()
    print(json.dumps(res, indent=2))
