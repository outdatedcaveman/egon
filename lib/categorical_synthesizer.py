"""Categorical Synthesizer — Natural language translation to ACT schemas using Gemini API.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx
from lib import secrets

_ROOT = Path(__file__).resolve().parent.parent


def synthesize_category(concept: str) -> dict[str, Any]:
    """Translates a natural language description of a concept into a formal
    category schema using Gemini API, writes it to markdown, and reconciles.
    """
    # 1. Resolve API key
    api_key = secrets.get("llm.api_key") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return {
            "status": "error",
            "error": "Gemini API key is not configured. Please add the 'llm.api_key' to your egon-config.json or define GEMINI_API_KEY in your environment."
        }

    # 2. Formulate the prompt
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

    # 3. Call Gemini API
    # Using gemini-2.5-flash as the fast, direct JSON model
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
    except Exception as e:
        return {
            "status": "error",
            "error": f"Failed to query Gemini API or parse JSON: {e}"
        }

    # 4. Validate output schema structure
    name = (cat_schema.get("name") or "Concept").strip()
    objects = cat_schema.get("objects") or []
    morphisms = cat_schema.get("morphisms") or []

    if not objects:
        return {
            "status": "error",
            "error": "Generated category has no objects. Try refining your description."
        }

    # 5. Format to markdown codeblock
    mor_lines = []
    for m in morphisms:
        dom = m.get("dom")
        codom = m.get("codom")
        lbl = m.get("label")
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
        "saved_path": str(filename),
        "reconcile": reconcile_res
    }
