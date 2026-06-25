"""Categorical Synthesizer — Natural language translation to ACT schemas using Gemini API.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from lib.lazy_httpx import httpx  # deferred ~2s import (2026-06-11 perf pass)
from lib import secrets

_ROOT = Path(__file__).resolve().parent.parent


def synthesize_category(concept: str) -> dict[str, Any]:
    """Translates a natural language description of a concept into a formal
    category schema using local VibeThinker (Ollama) or Gemini API.
    """
    # 1. Formulate the prompt
    prompt = f"""You are a formal mathematical modeling assistant specializing in Applied Category Theory (ACT).
Your task is to translate the user's natural language description of a system, process, or concept into a formal category representation in JSON.

The category consists of:
- A name (alphanumeric, singular, e.g. "LibrarySystem", "Epidemiology").
- Objects (the core concepts, entities, or states, e.g. "Book", "Host").
- Morphisms (directed relationships or actions between objects, e.g. "borrows" from Reader to Book).

Provide your output in the following JSON schema:
{{
  "name": "CategoryName",
  "objects": ["ObjectA", "ObjectB", "ObjectC"],
  "morphisms": [
    {{"dom": "ObjectA", "codom": "ObjectB", "label": "relationshipName"}},
    {{"dom": "ObjectB", "codom": "ObjectC", "label": "anotherRelationship"}}
  ]
}}

Ensure:
1. Object names are singular nouns, matching the casing (e.g. CamelCase).
2. Morphisms specify the domain (dom), codomain (codom), and a short lowercase relationship label.
3. Every object referenced in morphisms must be present in the objects list.
4. Do NOT include markdown codeblocks or explanation outside of the valid JSON object.

User concept to translate: "{concept}"
"""

    # 2. Check if local Ollama has VibeThinker
    ollama_host = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
    has_vibethinker = False
    vibethinker_model = ""
    try:
        with httpx.Client(timeout=2.0) as client:
            r = client.get(f"{ollama_host}/api/tags")
            if r.status_code == 200:
                models = [m["name"] for m in r.json().get("models", [])]
                for m in models:
                    if "vibethinker" in m.lower():
                        has_vibethinker = True
                        vibethinker_model = m
                        break
    except Exception:
        pass

    cat_schema = None
    source_model = None

    if has_vibethinker:
        # Use VibeThinker locally
        try:
            with httpx.Client(timeout=60.0) as client:
                resp = client.post(
                    f"{ollama_host}/api/generate",
                    json={
                        "model": vibethinker_model,
                        "prompt": prompt,
                        "format": "json",
                        "stream": False,
                        "options": {
                            "temperature": 1.0,
                            "top_p": 0.95
                        }
                    }
                )
            if resp.status_code == 200:
                text_content = resp.json()["response"].strip()
                cat_schema = json.loads(text_content)
                source_model = vibethinker_model
            else:
                raise Exception(f"Ollama returned HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            # Fall back to Gemini if Ollama fails
            pass

    if not cat_schema:
        # 3. Fall back to Gemini API
        api_key = secrets.get("llm.api_key") or os.environ.get("GEMINI_API_KEY")
        if not api_key:
            return {
                "status": "error",
                "error": "Failed to query local VibeThinker, and Gemini API key is not configured. Please add the 'llm.api_key' to your egon-config.json or define GEMINI_API_KEY in your environment."
            }

        url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
        params = {"key": api_key}
        headers = {"Content-Type": "application/json"}
        payload = {
            "contents": [{
                "parts": [{"text": prompt}]
            }],
            "generationConfig": {
                "responseMimeType": "application/json"
            }
        }

        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.post(url, params=params, json=payload, headers=headers)
            
            if resp.status_code != 200:
                return {
                    "status": "error",
                    "error": f"Gemini API returned HTTP {resp.status_code}: {resp.text[:300]}"
                }
            
            resp_json = resp.json()
            text_content = resp_json["candidates"][0]["content"]["parts"][0]["text"].strip()
            cat_schema = json.loads(text_content)
            source_model = "gemini-2.5-flash"
        except Exception as e:
            return {
                "status": "error",
                "error": f"Failed to query Gemini API or parse JSON: {e}"
            }

    # 4. Validate output schema structure
    if not isinstance(cat_schema, dict):
        return {
            "status": "error",
            "error": f"Generated category schema is not a JSON object: {cat_schema}"
        }

    name = cat_schema.get("name")
    if not isinstance(name, str):
        name = str(name) if name is not None else "Concept"
    name = name.strip() or "Concept"

    raw_objects = cat_schema.get("objects") or []
    objects = []
    if isinstance(raw_objects, list):
        for obj in raw_objects:
            if isinstance(obj, str):
                objects.append(obj.strip())
            elif obj is not None:
                objects.append(str(obj).strip())
    elif isinstance(raw_objects, dict):
        objects = [str(k).strip() for k in raw_objects.keys()]

    if not objects:
        return {
            "status": "error",
            "error": "Generated category has no objects. Try refining your description.",
            "raw_schema": cat_schema,
            "model": source_model
        }

    # 5. Format to markdown codeblock
    import re
    mor_lines = []
    raw_morphisms = cat_schema.get("morphisms") or []
    
    if not isinstance(raw_morphisms, list):
        if isinstance(raw_morphisms, dict):
            raw_morphisms = [raw_morphisms]
        else:
            raw_morphisms = [str(raw_morphisms)]

    for m in raw_morphisms:
        dom, codom, lbl = None, None, None
        if isinstance(m, dict):
            dom = m.get("dom")
            codom = m.get("codom")
            lbl = m.get("label") or m.get("lbl") or m.get("relationship")
            if dom is not None:
                dom = str(dom).strip()
            if codom is not None:
                codom = str(codom).strip()
            if lbl is not None:
                lbl = str(lbl).strip()
        elif isinstance(m, str):
            # Format: DomObj -> CodomObj: label
            match = re.match(r"([A-Za-z0-9_.-]+)\s*->\s*([A-Za-z0-9_.-]+)\s*:\s*([A-Za-z0-9_.-]+)", m.strip())
            if match:
                dom = match.group(1).strip()
                codom = match.group(2).strip()
                lbl = match.group(3).strip()
        
        if dom and codom and lbl:
            mor_lines.append(f"  {dom} -> {codom}: {lbl}")

    morphisms_block = "morphisms:\n" + "\n".join(mor_lines) if mor_lines else ""

    md_content = f"""# Category: {name}

Generated dynamically from natural language description:
> "{concept}"

```category
name: {name}
objects: {', '.join(objects)}
{morphisms_block}
```
"""

    # 6. Save to state/panop/ generated directory
    gen_dir = _ROOT / "state" / "panop"
    gen_dir.mkdir(parents=True, exist_ok=True)
    filename = gen_dir / f"categorical_{name.lower()}.md"
    
    try:
        filename.write_text(md_content, encoding="utf-8")
    except Exception as e:
        return {
            "status": "error",
            "error": f"Failed to save generated category markdown to disk: {e}"
        }

    # 7. Run Egon categorical scan & reconcile
    try:
        from lib.categorical_mind import scan_and_reconcile_categories
        reconcile_res = scan_and_reconcile_categories()
    except Exception as e:
        reconcile_res = {"error": str(e)}

    return {
        "status": "ok",
        "category": cat_schema,
        "model": source_model,
        "saved_path": str(filename),
        "reconcile": reconcile_res
    }
