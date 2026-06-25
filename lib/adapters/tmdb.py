"""TMDB enrichment — film metadata Letterboxd doesn't expose.

Letterboxd gives us title/year/rating/poster-page. TMDB fills the rest:
director, top-billed cast, genres, runtime, synopsis, and a clean poster
from its image CDN.

Auth: either a v3 API key (`tmdb.api_key`) or a v4 read-access bearer token
(`tmdb.token`). Both come from themoviedb.org/settings/api. We prefer the
bearer token if present.

Performance: enrichment is cached to disk keyed by "title|year" so we hit the
network ONCE per film, ever. A 500-film library costs ~1000 calls on first
build (search + details), then zero. Cache lives at state/tmdb_cache.json.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from lib.lazy_httpx import httpx  # deferred ~2s import (2026-06-11 perf pass)

from lib import secrets

_API = "https://api.themoviedb.org/3"
_IMG = "https://image.tmdb.org/t/p/w500"
_CACHE_FILE = Path(__file__).resolve().parent.parent.parent / "state" / "tmdb_cache.json"
_LOCK = threading.Lock()
_MEM_CACHE: dict | None = None


def _auth_kwargs() -> dict | None:
    """Return httpx kwargs for auth — bearer token preferred, else api_key param."""
    token = secrets.get("tmdb.token")
    if token:
        return {"headers": {"Authorization": f"Bearer {token}",
                            "accept": "application/json"}}
    key = secrets.get("tmdb.api_key")
    if key:
        return {"params_extra": {"api_key": key}}
    return None


def configured() -> bool:
    return bool(secrets.get("tmdb.token") or secrets.get("tmdb.api_key"))


def _load_cache() -> dict:
    global _MEM_CACHE
    if _MEM_CACHE is not None:
        return _MEM_CACHE
    try:
        _MEM_CACHE = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        _MEM_CACHE = {}
    return _MEM_CACHE


def _save_cache() -> None:
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(json.dumps(_MEM_CACHE or {}, ensure_ascii=False),
                               encoding="utf-8")
    except Exception:
        pass


def live_status() -> dict:
    if not configured():
        return {"status": "unconfigured",
                "error": "Paste a TMDB API key or v4 token (themoviedb.org/settings/api)."}
    auth = _auth_kwargs()
    try:
        params = {"query": "inception", "year": "2010"}
        if "params_extra" in auth:
            params.update(auth["params_extra"])
            r = httpx.get(f"{_API}/search/movie", params=params, timeout=10.0, verify=False)
        else:
            r = httpx.get(f"{_API}/search/movie", params=params,
                          headers=auth["headers"], timeout=10.0, verify=False)
        if r.status_code == 200:
            n = len(_load_cache())
            return {"status": "ok", "source": "tmdb",
                    "note": f"Authenticated. {n} films cached."}
        return {"status": "error", "error": f"TMDB HTTP {r.status_code} — check key/token."}
    except Exception as e:
        return {"status": "error", "error": str(e)[:160]}


def _get(path: str, params: dict | None = None) -> dict | None:
    auth = _auth_kwargs()
    if not auth:
        return None
    params = dict(params or {})
    try:
        if "params_extra" in auth:
            params.update(auth["params_extra"])
            r = httpx.get(f"{_API}{path}", params=params, timeout=12.0, verify=False)
        else:
            r = httpx.get(f"{_API}{path}", params=params,
                          headers=auth["headers"], timeout=12.0, verify=False)
        if r.status_code == 200:
            return r.json()
    except Exception:
        return None
    return None


def enrich(title: str, year: str = "") -> dict:
    """Return {poster, director, cast, genres, runtime, overview, tmdb_url}
    for one film. Cached to disk — only hits the network on first lookup.
    Empty dict if not configured or no match found.
    """
    if not configured() or not title:
        return {}
    key = f"{title.strip().lower()}|{str(year)[:4]}"
    cache = _load_cache()
    if key in cache:
        return cache[key]

    result: dict = {}
    # 1. search
    params = {"query": title}
    if year:
        params["year"] = str(year)[:4]
    search = _get("/search/movie", params)
    hits = (search or {}).get("results") or []
    if not hits and year:
        # Fallback: try searching without the year constraint in case of year mismatch
        params_no_year = {"query": title}
        search = _get("/search/movie", params_no_year)
        hits = (search or {}).get("results") or []
    if hits:
        movie_id = hits[0].get("id")
        # 2. details + credits
        det = _get(f"/movie/{movie_id}", {"append_to_response": "credits"}) or {}
        crew = (det.get("credits") or {}).get("crew") or []
        cast = (det.get("credits") or {}).get("cast") or []
        directors = [c.get("name") for c in crew if c.get("job") == "Director"]
        # language: ISO code → display name; country from production_countries
        _LANG = {"en": "English", "fr": "French", "es": "Spanish", "pt": "Portuguese",
                 "de": "German", "it": "Italian", "ja": "Japanese", "ko": "Korean",
                 "zh": "Chinese", "ru": "Russian", "sv": "Swedish", "da": "Danish",
                 "fi": "Finnish", "no": "Norwegian", "nl": "Dutch", "pl": "Polish",
                 "hi": "Hindi", "ar": "Arabic", "fa": "Persian", "tr": "Turkish",
                 "cs": "Czech", "hu": "Hungarian", "el": "Greek", "th": "Thai"}
        lang_code = det.get("original_language", "")
        countries = [c.get("name", "") for c in (det.get("production_countries") or [])]
        try:
            rating = round(float(det.get("vote_average") or 0), 1)
        except Exception:
            rating = ""
        result = {
            "poster":   (_IMG + det["poster_path"]) if det.get("poster_path") else "",
            "director": ", ".join(d for d in directors if d),
            "cast":     ", ".join(c.get("name", "") for c in cast[:5]),
            "genres":   ", ".join(g.get("name", "") for g in (det.get("genres") or [])),
            "runtime":  det.get("runtime") or "",
            "overview": (det.get("overview") or "")[:500],
            "tmdb_url": f"https://www.themoviedb.org/movie/{movie_id}",
            "tmdb_rating": rating,
            "language": _LANG.get(lang_code, lang_code.upper() if lang_code else ""),
            "country":  ", ".join(countries[:2]),
        }
    # cache even empty results so we don't re-query misses every load
    with _LOCK:
        cache[key] = result
        _save_cache()
    return result


def enrich_tv_show(tvdb_id: str | int) -> dict:
    """Find a TV show by its TVDB ID on TMDB and return its metadata (poster, etc.).
    Cached to disk keyed by 'tvdb|<tvdb_id>'.
    """
    if not configured() or not tvdb_id:
        return {}
    key = f"tvdb|{tvdb_id}"
    cache = _load_cache()
    if key in cache:
        return cache[key]

    result: dict = {}
    data = _get(f"/find/{tvdb_id}", {"external_source": "tvdb_id"})
    tv_results = (data or {}).get("tv_results") or []
    if tv_results:
        show = tv_results[0]
        result = {
            "poster": (_IMG + show["poster_path"]) if show.get("poster_path") else "",
            "title": show.get("name") or "",
            "overview": (show.get("overview") or "")[:500],
            "tmdb_rating": show.get("vote_average") or "",
            "year": (show.get("first_air_date") or "")[:4],
        }
    with _LOCK:
        cache[key] = result
        _save_cache()
    return result
