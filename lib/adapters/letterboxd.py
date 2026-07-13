"""Letterboxd — full reader: ALL films via export ZIP + lists via public page.

Letterboxd's WAF blocks `/films/page/N/` requests, so we can't scrape the full
list. Two paths:

1. **Export ZIP (recommended for full corpus)** — download from
   https://letterboxd.com/data/export/ (one click; needs you logged in).
   Set `letterboxd.export_path` in egon-config.json to point at the ZIP.
   Contains every film you've watched, every list, every diary entry, ratings.

2. **Public-page fallback (no auth, partial)** — scrapes the first page
   (most recent 72 watched). Used when no export ZIP is configured.

Lists always come from the public /lists/ page (not blocked).
"""
from __future__ import annotations

import csv
import io
import re
import time
import zipfile
from datetime import datetime
from pathlib import Path

from lib.lazy_httpx import httpx  # deferred ~2s import (2026-06-11 perf pass)
from bs4 import BeautifulSoup

from lib import secrets
from lib.ledger import load_config, save_config
from lib.snapshot_store import latest_snapshot

META = {
    "id": "letterboxd",
    "label": "Letterboxd",
    "icon": "🎬",
    "kind": "media",
    "needs_auth": False,
    "destructive_actions": [],
    "read_only_default": True,
}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
PAGE_DELAY_S = 0.6


def _username() -> str | None:
    return secrets.get("letterboxd.username")


def _rss_diary(limit: int = 50) -> list[dict]:
    """Last ~50 diary entries from the PUBLIC RSS feed — the live freshness
    source. The export ZIP is a one-time download that goes stale the day
    after (Bruno 2026-07-12: 'no update on my letterboxd viewed'); the RSS
    needs no auth and carries watched dates + ratings."""
    user = _username()
    if not user:
        return []
    try:
        import re as _re
        r = httpx.get(f"https://letterboxd.com/{user}/rss/", timeout=15,
                      follow_redirects=True, headers={"User-Agent": UA})
        if r.status_code != 200:
            return []
        out = []
        for m in _re.finditer(r"<item>(.*?)</item>", r.text, _re.S):
            b = m.group(1)

            def tag(t, _b=b):
                mm = _re.search(rf"<{t}>([^<]*)</{t}>", _b)
                return mm.group(1).strip() if mm else ""

            title = tag("letterboxd:filmTitle")
            if not title:
                continue
            link = tag("link")
            mm = _re.search(r'src="([^"]+)"', b)
            out.append({
                "title": title,
                "year": tag("letterboxd:filmYear"),
                "watched": tag("letterboxd:watchedDate"),
                "rating": tag("letterboxd:memberRating"),
                "url": link,
                "poster": mm.group(1) if mm else "",
                "slug": link.rstrip("/").split("/")[-1] if link else "",
                "kind": "diary",
            })
        return out[:limit]
    except Exception:
        return []


def _get(client: httpx.Client, url: str) -> BeautifulSoup | None:
    try:
        r = client.get(url, timeout=20.0, follow_redirects=True)
        if r.status_code != 200:
            return None
        return BeautifulSoup(r.text, "lxml")
    except Exception:
        return None


def _session_cookie() -> str | None:
    """Cached session cookie. Saved automatically after a successful login."""
    return secrets.get("letterboxd.session_cookie")


def _password() -> str | None:
    return secrets.get("letterboxd.password")


def _try_auto_login() -> tuple[bool, str]:
    """POST username+password to Letterboxd sign-in endpoint, persist cookies.

    Letterboxd's sign-in flow:
      1. GET / → server sets a CSRF cookie. The cookie name has changed
         over the years (`com.xk72.webparts.csrf` historically, now sometimes
         `CSRF-TOKEN` or it's only available as a meta tag on the HTML). We
         probe ALL plausible cookie names AND fall back to parsing the meta
         tag if no cookie is set.
      2. POST /user/login.do with __csrf, username, password.
      3. Server returns {"result":"success"} and sets letterboxd.signature.

    Returns (True, "...") on success, (False, "<reason>") on any failure.
    The reason string is what gets surfaced in the Settings panel — keep it
    human-readable.
    """
    u = _username(); p = _password()
    if not (u and p):
        return False, "missing username or password"
    client = httpx.Client(
        headers={"User-Agent": UA,
                 "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                 "Accept-Language": "en-US,en;q=0.9"},
        timeout=20.0, follow_redirects=True,
    )
    try:
        # 1. Hit the homepage so the server sets its CSRF cookie / embeds it
        #    in the HTML.
        r0 = client.get("https://letterboxd.com/")
        if r0.status_code != 200:
            return False, f"homepage HTTP {r0.status_code}"

        # 2. Resolve the CSRF token by trying every known location.
        csrf = None
        # 2a. Probe cookie jar with multiple known names + any cookie whose
        #     name contains 'csrf' (case-insensitive).
        cookie_candidates = (
            "com.xk72.webparts.csrf",
            "CSRF-TOKEN",
            "csrftoken",
            "csrf",
        )
        for name in cookie_candidates:
            if client.cookies.get(name):
                csrf = client.cookies.get(name)
                break
        if not csrf:
            for k, v in client.cookies.items():
                if "csrf" in k.lower() and v:
                    csrf = v
                    break
        # 2b. Fall back to parsing the HTML — Letterboxd embeds the CSRF token
        #     as <meta name="csrf-token" content="..."> on every page when the
        #     cookie path is disabled.
        if not csrf:
            m = re.search(
                r'name=["\']csrf[-_]?token["\'][^>]*content=["\']([^"\']+)["\']',
                r0.text, flags=re.IGNORECASE)
            if m:
                csrf = m.group(1)
        # 2c. Last-ditch: look for the input the login form itself uses.
        if not csrf:
            m = re.search(
                r'name=["\']__csrf["\'][^>]*value=["\']([^"\']+)["\']',
                r0.text, flags=re.IGNORECASE)
            if m:
                csrf = m.group(1)

        if not csrf:
            return False, ("no CSRF token in cookies or HTML — Letterboxd may "
                           "have changed their auth flow. Use the export-ZIP "
                           "fallback (set letterboxd.export_path).")

        # 2. submit login
        r = client.post(
            "https://letterboxd.com/user/login.do",
            data={"__csrf": csrf, "username": u, "password": p, "remember": "true"},
            headers={"Referer": "https://letterboxd.com/",
                     "Origin": "https://letterboxd.com",
                     "X-Requested-With": "XMLHttpRequest"},
        )
        if r.status_code != 200:
            return False, f"login HTTP {r.status_code}"
        # response body should contain {"result":"success"}
        if '"result":"success"' not in r.text:
            return False, f"login failed: {r.text[:200]}"

        # 3. assemble Cookie header from the session jar
        cookie_str = "; ".join(f"{k}={v}" for k, v in client.cookies.items())
        if "letterboxd.signature" not in cookie_str:
            return False, "no signature cookie after login"

        # 4. persist to config (gitignored)
        cfg = load_config()
        cfg.setdefault("letterboxd", {})["session_cookie"] = cookie_str
        save_config(cfg)
        return True, "logged in, session cookie cached"
    except Exception as e:
        return False, str(e)
    finally:
        client.close()


def _build_client() -> httpx.Client:
    """Build a client; auto-login if password configured and no cookie yet (or expired)."""
    headers = {"User-Agent": UA}
    sc = _session_cookie()
    if sc:
        headers["Cookie"] = sc.strip()
    return httpx.Client(headers=headers, timeout=20.0)


# -- films ------------------------------------------------------------------

_RATED_RE = re.compile(r"rated-(\d+)")
_YEAR_RE  = re.compile(r"\((\d{4})\)\s*$")


def _poster_url_from_slug(slug: str) -> str:
    """Letterboxd's poster endpoint — returns a 230×345 jpg.

    URL pattern is stable: /film/<slug>/image-150/ → 230x345 jpg.
    The Letterboxd CDN itself returns the actual image bytes.
    """
    return f"https://letterboxd.com/film/{slug}/image-150/" if slug else ""


def _slug_from_title(title: str, year: str = "") -> str:
    """Heuristic Letterboxd slug. Reasonable for ~80% of titles.

    Letterboxd: lowercase, NFKD-normalize, strip non-alnum, dashes.
    Year suffix added for disambiguation (we don't know which — try without first).
    """
    import unicodedata as _u
    s = _u.normalize("NFKD", title or "").encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-zA-Z0-9\s-]", "", s).lower().strip()
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s


def _parse_films_page(soup: BeautifulSoup) -> list[dict]:
    out = []
    for li in soup.select("li.griditem, li.poster-container"):
        # 2026+ markup: data on <div class="react-component">
        comp = li.select_one('div.react-component[data-item-link], div.react-component[data-item-slug]')
        slug = title = year = link = poster = ""
        if comp:
            slug = (comp.get("data-item-slug") or "").strip()
            full = (comp.get("data-item-full-display-name")
                    or comp.get("data-item-name") or "").strip()
            link = (comp.get("data-item-link") or "").strip()
            # The data-poster-url attribute points at the image endpoint we want.
            poster = (comp.get("data-poster-url") or "").strip()
            # full = "Title (YEAR)"
            ym = _YEAR_RE.search(full)
            if ym:
                year = ym.group(1)
                title = full[:ym.start()].rstrip()
            else:
                title = full
        else:
            poster_el = li.select_one("div.film-poster, div.poster")
            if poster_el:
                slug  = poster_el.get("data-film-slug") or ""
                title = (poster_el.get("data-film-name") or "").strip()
                year  = poster_el.get("data-film-release-year") or ""
                link  = poster_el.get("data-target-link") or ""

        if not poster and slug:
            poster = _poster_url_from_slug(slug)

        # rating: <span class="rating ... rated-N"> where N is 1..10 (each = 0.5★)
        rated = li.select_one("span.rating[class*='rated-']")
        rating = None
        if rated:
            m = _RATED_RE.search(" ".join(rated.get("class", [])))
            if m:
                try:
                    rating = round(int(m.group(1)) / 2, 1)
                except ValueError:
                    pass

        liked = bool(li.select_one("span.like, .icon-liked, .has-icon-liked"))

        if not slug and not title:
            continue

        # Make poster URL absolute
        if poster and poster.startswith("/"):
            poster = f"https://letterboxd.com{poster}"

        out.append({
            "slug":   slug,
            "title":  title,
            "year":   year,
            "rating": rating,
            "liked":  liked,
            "poster": poster,
            "url":    f"https://letterboxd.com{link}" if link else
                      (f"https://letterboxd.com/film/{slug}/" if slug else None),
        })
    return out


def _fetch_films_full(client: httpx.Client, username: str, max_pages: int = 80) -> list[dict]:
    """Paginated scrape with session cookie. Stops when a page is empty or 403."""
    films: list[dict] = []
    for page in range(1, max_pages + 1):
        url = (f"https://letterboxd.com/{username}/films/"
               if page == 1
               else f"https://letterboxd.com/{username}/films/page/{page}/")
        try:
            r = client.get(url, timeout=20.0, follow_redirects=True,
                           headers={"Referer": f"https://letterboxd.com/{username}/films/"})
        except Exception:
            break
        if r.status_code != 200:
            break
        soup = BeautifulSoup(r.text, "lxml")
        batch = _parse_films_page(soup)
        if not batch:
            break
        films.extend(batch)
        if len(batch) < 70:
            break
        time.sleep(PAGE_DELAY_S)
    return films


def _fetch_recent_films(client: httpx.Client, username: str) -> list[dict]:
    """Backwards-compat: only the first 72."""
    soup = _get(client, f"https://letterboxd.com/{username}/films/")
    return _parse_films_page(soup) if soup else []


# -- export ZIP ingest -------------------------------------------------------

def _load_export_zip(path: Path) -> dict:
    """Parse a Letterboxd export ZIP. Returns {films, diary, ratings, lists}."""
    if not path.exists():
        return {"error": f"export zip not found: {path}"}

    def _read_csv(zf: zipfile.ZipFile, name: str) -> list[dict]:
        try:
            with zf.open(name) as f:
                text = io.TextIOWrapper(f, encoding="utf-8")
                return list(csv.DictReader(text))
        except KeyError:
            return []

    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        watched  = _read_csv(zf, "watched.csv")
        diary    = _read_csv(zf, "diary.csv")
        ratings  = _read_csv(zf, "ratings.csv")
        likes    = _read_csv(zf, "likes/films.csv")
        # lists are in lists/<list-slug>.csv  + metadata in lists.csv. If lists.csv does not exist,
        # we parse them directly from each list file.
        lists_meta = []
        list_contents: dict[str, list[dict]] = {}
        for n in names:
            if n.startswith("lists/") and n.endswith(".csv"):
                slug = Path(n).stem
                try:
                    with zf.open(n) as lf:
                        content_bytes = lf.read()
                        content_text = content_bytes.decode("utf-8")
                        lines = content_text.splitlines()
                        
                        list_name = slug.replace("-", " ").title()
                        list_url = ""
                        list_desc = ""
                        list_tags = ""
                        
                        if len(lines) >= 3 and "," in lines[1]:
                            headers = [h.strip() for h in lines[1].split(",")]
                            val_reader = csv.reader([lines[2]])
                            vals = next(val_reader, [])
                            meta_dict = dict(zip(headers, vals))
                            list_name = meta_dict.get("Name") or list_name
                            list_url = meta_dict.get("URL") or ""
                            list_desc = meta_dict.get("Description") or ""
                            list_tags = meta_dict.get("Tags") or ""
                            
                        items_start_idx = -1
                        for idx, line in enumerate(lines):
                            if line.startswith("Position,") or "Position,Name,Year" in line:
                                items_start_idx = idx
                                break
                                
                        items_list = []
                        if items_start_idx != -1:
                            items_text = "\n".join(lines[items_start_idx:])
                            reader = csv.DictReader(io.StringIO(items_text))
                            for row in reader:
                                items_list.append({
                                    "position": row.get("Position"),
                                    "title": row.get("Name"),
                                    "year": row.get("Year"),
                                    "url": row.get("URL"),
                                    "description": row.get("Description"),
                                })
                                
                        list_contents[slug] = items_list
                        lists_meta.append({
                            "Name": list_name,
                            "URL": list_url,
                            "Tags": list_tags,
                            "Description": list_desc,
                            "slug": slug,
                        })
                except Exception:
                    pass

    # canonicalize: index films by (name, year)
    by_key = {}
    for row in watched:
        key = (row.get("Name", ""), row.get("Year", ""))
        url = row.get("Letterboxd URI", "")
        # derive slug from the URL: https://letterboxd.com/film/<slug>/
        slug = ""
        if url and "/film/" in url:
            slug = url.split("/film/", 1)[1].rstrip("/").split("/")[0]
        # Letterboxd's export uses boxd.it short URLs — fall back to deriving
        # slug from the title (works for ~80%; year-disambiguated titles miss).
        if not slug:
            slug = _slug_from_title(row.get("Name", ""), row.get("Year", ""))
        by_key[key] = {
            "title": row.get("Name", ""),
            "year":  row.get("Year", ""),
            "watched_date": row.get("Date", ""),
            "rating": None,
            "liked":  False,
            "url":    url,
            "slug":   slug,
            "poster": _poster_url_from_slug(slug),
        }
    for row in ratings:
        key = (row.get("Name", ""), row.get("Year", ""))
        if key in by_key:
            try: by_key[key]["rating"] = float(row.get("Rating", "") or 0) or None
            except ValueError: pass
    for row in likes:
        key = (row.get("Name", ""), row.get("Year", ""))
        if key in by_key:
            by_key[key]["liked"] = True

    films = list(by_key.values())
    # lists merge: include both pure metadata + content counts
    lists = []
    for row in lists_meta:
        slug = row.get("slug") or (row.get("URL", "").rstrip("/").rsplit("/", 1) + [""])[-1] if row.get("URL") else ""
        lists.append({
            "name":  row.get("Name", ""),
            "url":   row.get("URL", ""),
            "tags":  row.get("Tags", ""),
            "count": len(list_contents.get(slug, [])),
            "description": row.get("Description", "")[:300],
            "items": list_contents.get(slug, []),
        })
    return {
        "films": films, "diary": diary, "ratings": ratings, "likes": likes,
        "lists": lists,
    }


# -- lists ------------------------------------------------------------------

def _fetch_lists(client: httpx.Client, username: str, max_pages: int = 20) -> list[dict]:
    lists: list[dict] = []
    for page in range(1, max_pages + 1):
        url = f"https://letterboxd.com/{username}/lists/page/{page}/"
        soup = _get(client, url)
        if not soup:
            break
        new = 0
        for sec in soup.select("section.list-set article.list-summary, article.list-summary"):
            a = sec.select_one("h2 a, h2.title-2 a")
            if not a:
                continue
            href = a.get("href", "").strip("/")
            name = a.get_text(strip=True)
            count_el = sec.select_one(".list-attributes, .value")
            count_text = count_el.get_text(strip=True) if count_el else ""
            m = re.search(r"(\d[\d,]*)", count_text)
            count = int(m.group(1).replace(",", "")) if m else None
            desc_el = sec.select_one(".body-text, .summary")
            lists.append({
                "name":  name,
                "slug":  href,
                "url":   f"https://letterboxd.com/{href}/",
                "count": count,
                "description": desc_el.get_text(strip=True)[:300] if desc_el else "",
            })
            new += 1
        if new == 0:
            break
        time.sleep(PAGE_DELAY_S)
    return lists


# -- public API -------------------------------------------------------------

def auto_login() -> dict:
    """Dict-shaped wrapper around _try_auto_login() for the Settings Test button."""
    ok, msg = _try_auto_login()
    return {"status": "ok" if ok else "error", "error": None if ok else msg, "detail": msg}


def live_status() -> dict:
    u = _username()
    if not u:
        return {"status": "unconfigured", "error": "set letterboxd.username in egon-config.json"}
    try:
        r = httpx.get(f"https://letterboxd.com/{u}/",
                      timeout=5.0, follow_redirects=True, headers={"User-Agent": UA})
        if r.status_code == 200:
            return {"status": "ok", "username": u, "note":
                    "scraping limited to first page; configure letterboxd.export_path for full corpus"}
        return _cached_or(f"HTTP {r.status_code}")
    except Exception as e:
        return _cached_or(str(e))


def _cached_or(err: str) -> dict:
    """The films come from the export/snapshot, not this liveness ping — so a
    slow ping shouldn't degrade a source whose data is fresh. Bruno 2026-07-06."""
    try:
        from lib.source_health import has_recent_data
        if has_recent_data("letterboxd"):
            return {"status": "ok", "note": f"cached corpus (live ping slow: {err[:50]})"}
    except Exception:
        pass
    return {"status": "error", "error": err[:120]}


def snapshot() -> dict:
    u = _username()
    if not u:
        return {"status": "unconfigured", "error": "no username"}

    # 1) try the export ZIP if configured — this is the FULL corpus
    export_path = secrets.get("letterboxd.export_path")
    export_data = None
    if export_path:
        p = Path(export_path)
        # Auto-detect newer ZIP exports in the same folder
        try:
            parent = p.parent
            if parent.exists():
                zips = sorted(parent.glob("letterboxd-*.zip"), key=lambda x: x.stat().st_mtime, reverse=True)
                valid_zips = [z for z in zips if z.stat().st_size > 1000]
                if valid_zips and valid_zips[0].resolve() != p.resolve():
                    p = valid_zips[0]
                    secrets.set("letterboxd.export_path", str(p.resolve()))
                    cfg = load_config()
                    cfg.setdefault("letterboxd", {})["export_path"] = str(p.resolve())
                    save_config(cfg)
        except Exception:
            pass

        if p.exists():
            export_data = _load_export_zip(p)
            if "error" in export_data:
                export_data = None

    # 2) if password is configured but no cookie yet (or it expired), auto-login
    if _password() and not _session_cookie():
        _try_auto_login()

    use_cookie = _session_cookie() is not None
    with _build_client() as client:
        try:
            films = (_fetch_films_full(client, u) if use_cookie
                     else _fetch_recent_films(client, u))
            public_lists = _fetch_lists(client, u)
        except Exception as e:
            films, public_lists = [], []
            if not export_data:
                return {"status": "error", "error": str(e)}

    # PRE-WARM TMDB CACHE here:
    # Check uncached films and resolve a small batch (e.g. 30 films) during the background snapshot pass.
    # This slowly warms up the cache without slowing down the UI thread.
    try:
        from lib.adapters import tmdb
        if tmdb.configured():
            t_cache = tmdb._load_cache()
            all_films = export_data["films"] if export_data else films
            uncached = []
            for f in all_films:
                t, y = f.get("title", ""), f.get("year", "")
                if t:
                    ckey = f"{t.strip().lower()}|{str(y)[:4]}"
                    if ckey not in t_cache:
                        uncached.append((t, y))
            # Enrich a small batch of 30 films per snapshot pass to avoid getting blocked
            for t, y in uncached[:30]:
                try:
                    tmdb.enrich(t, y)
                    time.sleep(0.2)  # small rate-limit politeness delay
                except Exception:
                    pass
    except Exception:
        pass

    if export_data:
        merged_films = list(export_data["films"])
        inserted = 0
        if films:
            existing_keys = {f"{f.get('title', '').strip().lower()}|{f.get('year', '')}" for f in merged_films}
            for f in reversed(films):
                t = f.get("title", "").strip().lower()
                y = f.get("year", "")
                key = f"{t}|{y}"
                if key not in existing_keys:
                    merged_films.insert(0, f)
                    inserted += 1
                    
        return {
            "status": "ok",
            "source": "export_zip" + (f" + merged {inserted} scraped films" if inserted else ""),
            "synced_at": datetime.now().isoformat(),
            "count": len(merged_films),
            "lists_count": len(export_data["lists"]),
            "items":  merged_films,
            "lists":  export_data["lists"],
            "grey_status": None,
            "diary_entries": len(export_data["diary"]),
            "recent_scrape": films,
        }
    return {
        "status": "ok",
        "source": "cookie_scrape" if use_cookie else "anon_scrape (first page only)",
        "synced_at": datetime.now().isoformat(),
        "count": len(films),
        "lists_count": len(public_lists),
        "items":  films,
        "lists":  public_lists,
    }


_ZIP_FILMS_CACHE: dict[str, tuple[float, list[dict]]] = {}
_ZIP_LISTS_CACHE: dict[str, tuple[float, list[dict]]] = {}


def items(limit: int = 5000) -> list[dict]:
    """Full film library. Reads the export ZIP DIRECTLY (live) when configured,
    merges any newly scraped films from the latest snapshot, and falls back to
    the snapshot store otherwise."""
    films = []
    export_path = secrets.get("letterboxd.export_path")
    if export_path:
        p = Path(export_path)
        if p.exists():
            mtime = p.stat().st_mtime
            cache_key = str(p.resolve())
            if cache_key in _ZIP_FILMS_CACHE and _ZIP_FILMS_CACHE[cache_key][0] == mtime:
                films = list(_ZIP_FILMS_CACHE[cache_key][1])
            else:
                data = _load_export_zip(p)
                if "error" not in data:
                    f_list = data.get("films") or []
                    _ZIP_FILMS_CACHE[cache_key] = (mtime, f_list)
                    films = list(f_list)
                    
    snap = latest_snapshot(META["id"])
    if snap and snap.get("status") == "ok":
        scraped = snap.get("recent_scrape") or snap.get("items") or []
        if scraped and films:
            existing_keys = {f"{f.get('title', '').strip().lower()}|{f.get('year', '')}" for f in films}
            for f in reversed(scraped):
                t = f.get("title", "").strip().lower()
                y = f.get("year", "")
                key = f"{t}|{y}"
                if key not in existing_keys:
                    films.insert(0, f)
        elif not films:
            films = snap.get("items", [])

    # LIVE freshness: merge the RSS diary — attach watched dates/ratings to
    # library films and surface anything logged AFTER the export ZIP was
    # downloaded (new watches appear at the top, with dates for recency sort).
    diary = _rss_diary()
    if diary:
        by_key = {f"{(f.get('title') or '').strip().lower()}|{f.get('year', '')}": f
                  for f in films}
        for d in reversed(diary):
            k = f"{(d.get('title') or '').strip().lower()}|{d.get('year', '')}"
            f = by_key.get(k)
            if f is not None:
                if d.get("watched") and not f.get("watched"):
                    f["watched"] = d["watched"]
                if d.get("rating") and not f.get("rating"):
                    f["rating"] = d["rating"]
            else:
                films.insert(0, dict(d))
                by_key[k] = films[0]

    return films[:limit]


def lists(limit: int = 1000) -> list[dict]:
    """Your Letterboxd lists — read live from the export ZIP (which contains
    every list + its film count), falling back to the snapshot store."""
    export_path = secrets.get("letterboxd.export_path")
    if export_path:
        p = Path(export_path)
        if p.exists():
            mtime = p.stat().st_mtime
            cache_key = str(p.resolve())
            if cache_key in _ZIP_LISTS_CACHE and _ZIP_LISTS_CACHE[cache_key][0] == mtime:
                return _ZIP_LISTS_CACHE[cache_key][1][:limit]
            data = _load_export_zip(p)
            if "error" not in data:
                lists_data = data.get("lists") or []
                _ZIP_LISTS_CACHE[cache_key] = (mtime, lists_data)
                return lists_data[:limit]
    snap = latest_snapshot(META["id"])
    return (snap.get("lists") or [])[:limit] if snap else []



def stats() -> dict:
    snap = latest_snapshot(META["id"])
    if not snap:
        return {"status": "no-snapshot", "count": 0, "last_synced": None,
                "error": "click Sync now"}
    return {
        "status": snap.get("status", "ok"),
        "count": snap.get("count", 0),
        "lists": snap.get("lists_count", 0),
        "last_synced": (snap.get("synced_at") or "")[:16],
    }
