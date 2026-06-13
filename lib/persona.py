"""Persona — the data foundation for Bruno's digital double.

Bruno 2026-06-12: a window that gathers the behavioural/personal data that
defines who he is — fitness, interests, media taste, reading — into one
profile an AI could use to represent or reason as him. This is the
aggregation layer; egon_app/pages/persona.py visualizes it, and a synthesis
pass (local LLM) can turn the numbers into prose.

Pure read: every figure comes from snapshots already in the store (Fit,
Discover, YouTube, Letterboxd, music, podcasts, Kindle, Zotero, Instapaper).
No new collection — the persona is a *lens* on data Egon already holds.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _snap(source: str) -> list[dict]:
    try:
        from lib import cross_search
        s = cross_search._latest_snapshot_for(source)
        return (s or {}).get("items") or []
    except Exception:
        return []


def _count(source: str) -> int:
    """Item count WITHOUT loading the whole items list (cheap for huge
    snapshots like zotero's 252k). Reads the snapshot's `count` field via a
    bounded JSON scan; falls back to len(_snap)."""
    try:
        from lib.egon_paths import STATE_DIR
        import glob
        files = sorted(glob.glob(str(STATE_DIR / "snapshots" / source / "*.json")))
        if files:
            with open(files[-1], encoding="utf-8") as f:
                head = f.read(4000)
            import re
            mt = re.search(r'"count"\s*:\s*(\d+)', head)
            if mt:
                return int(mt.group(1))
    except Exception:
        pass
    return len(_snap(source))


def _num(v) -> float:
    try:
        return float(v)
    except Exception:
        return 0.0


# ── health (Google Fit) ──────────────────────────────────────────────────────
def _health() -> dict:
    days = _snap("google_fit")
    if not days:
        return {"available": False}
    parsed = []
    for d in days:
        try:
            stats = json.loads(d.get("content") or "{}")
        except Exception:
            stats = {}
        parsed.append((d.get("when") or "", stats))
    parsed.sort()
    steps = [s.get("steps", 0) for _, s in parsed]
    total_steps = int(sum(steps))
    total_km = round(sum(_num(s.get("distance_m")) for _, s in parsed) / 1000, 1)
    total_kcal = int(sum(_num(s.get("calories_kcal")) for _, s in parsed))
    active = [d for d, s in parsed if s.get("steps", 0) > 0]
    recent = [s.get("steps", 0) for _, s in parsed[-30:]]
    best = max(parsed, key=lambda x: x[1].get("steps", 0), default=("", {}))
    return {
        "available": True,
        "days_tracked": len(active),
        "first_day": (active[0] if active else ""),
        "last_day": (active[-1] if active else ""),
        "lifetime_steps": total_steps,
        "lifetime_km": total_km,
        "lifetime_kcal": total_kcal,
        "avg_daily_steps": int(total_steps / len(active)) if active else 0,
        "recent30_avg_steps": int(sum(recent) / len(recent)) if recent else 0,
        "best_day": {"date": best[0], "steps": int(best[1].get("steps", 0))},
        "years": round(len(active) / 365.0, 1) if active else 0,
    }


# ── interests (Discover + YouTube subs) ──────────────────────────────────────
def _interests() -> dict:
    disc = _snap("google_discover")
    follows = [d["title"] for d in disc if "follow" in d.get("kind", "")]
    likes = [d["title"] for d in disc if "liked" in d.get("kind", "")]
    nope = [d["title"] for d in disc if "not_interested" in d.get("kind", "")]
    subs = [d["title"] for d in _snap("youtube_oauth")
            if d.get("kind") == "subscription"]
    return {
        "available": bool(disc or subs),
        "follows": follows[:40], "follows_count": len(follows),
        "likes_count": len(likes),
        "not_interested": nope[:40], "not_interested_count": len(nope),
        "youtube_subs": subs[:60], "subs_count": len(subs),
    }


# ── media taste ──────────────────────────────────────────────────────────────
def _media() -> dict:
    films = _snap("letterboxd")
    rated = [(f.get("title"), _num(f.get("rating"))) for f in films
             if f.get("rating")]
    rated.sort(key=lambda x: -x[1])
    music_n = _count("youtube_music")
    yt_liked = [d for d in _snap("youtube_oauth")
                if d.get("kind") == "liked_video"]
    pods_n = _count("pocketcasts")
    tv = _snap("tvtime")
    return {
        "films_watched": len(films),
        "films_top": [t for t, _ in rated[:12]],
        "films_liked": sum(1 for f in films if f.get("liked")),
        "music_tracks": music_n,
        "youtube_likes": len(yt_liked),
        "podcasts": pods_n,
        "tv_episodes": len(tv),
    }


# ── reading ──────────────────────────────────────────────────────────────────
def _reading() -> dict:
    return {
        "kindle_items": _count("kindle"),
        "zotero_refs": _count("zotero"),
        "instapaper": _count("instapaper"),
        "paperpile": _count("paperpile"),
        "bookmarks": _count("chrome_bookmarks"),
    }


def build_profile() -> dict:
    """The full persona profile — every section, computed from snapshots."""
    health = _health()
    interests = _interests()
    media = _media()
    reading = _reading()
    footprint = {
        "references": reading["zotero_refs"] + reading["paperpile"],
        "articles": reading["instapaper"] + reading["bookmarks"],
        "films": media["films_watched"],
        "music_tracks": media["music_tracks"],
        "fitness_days": health.get("days_tracked", 0),
        "subscriptions": interests.get("subs_count", 0),
    }
    return {"health": health, "interests": interests, "media": media,
            "reading": reading, "footprint": footprint}


def synthesize_prose(profile: dict | None = None) -> dict:
    """Optional: ask the local LLM to describe the person from the numbers.
    Used by the 'Generate digital-double summary' button. $0, on-device."""
    profile = profile or build_profile()
    h, i, m, r = (profile["health"], profile["interests"],
                  profile["media"], profile["reading"])
    facts = (
        f"Fitness: {h.get('days_tracked',0)} days tracked over {h.get('years',0)} "
        f"years, {h.get('lifetime_steps',0):,} lifetime steps, "
        f"{h.get('avg_daily_steps',0):,}/day average, recent 30d "
        f"{h.get('recent30_avg_steps',0):,}/day.\n"
        f"Interests: follows {', '.join(i.get('follows',[])[:15])}; "
        f"YouTube subscriptions include {', '.join(i.get('youtube_subs',[])[:15])}.\n"
        f"Media: {m.get('films_watched',0)} films "
        f"(favourites: {', '.join(m.get('films_top',[])[:8])}), "
        f"{m.get('music_tracks',0)} saved tracks, {m.get('podcasts',0)} podcasts.\n"
        f"Reading: {r.get('zotero_refs',0):,} academic references, "
        f"{r.get('kindle_items',0)} Kindle items, "
        f"{r.get('instapaper',0)} saved articles.")
    prompt = ("From the data below, write a concise, perceptive third-person "
              "profile of this person — their intellectual character, "
              "interests, habits and tastes — as grounding for an AI that "
              "represents them. 1-2 paragraphs, specific, no fluff.\n\n" + facts)
    try:
        from lib.synthesis import _chat, _config
        out = _chat(prompt, _config(), timeout=90.0)
        text = out if isinstance(out, str) else (out or "")
        if text and str(text).strip():
            return {"status": "ok", "summary": str(text).strip(), "facts": facts}
    except Exception:
        pass
    return {"status": "no_llm", "facts": facts,
            "summary": "(local LLM unavailable — the facts above are the raw "
                       "material; start Ollama to generate prose.)"}
