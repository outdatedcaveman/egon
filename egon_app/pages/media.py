"""Media page — visual. Films with posters, music with album art, shows.

Bruno's directive: media must be visual, pretty, complete and actionable.
Films: poster · year · my rating. Music: album art · artist · album · year.
Each tab is a PosterGridWidget; click a card to open it on the source site.
"""
from __future__ import annotations

import webbrowser

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QTabWidget

from egon_app.widgets import PosterGridWidget, SegmentedGridWidget, ItemListWidget


# ── Instapaper (saved articles — list view, no cover art) ───────────────────
# Bruno 2026-05-29: moved here from References. Articles have no posters, so
# it renders as a sortable/searchable table via ItemListWidget, not the
# poster grid. Data comes from the Chrome-extension harvest (Panop), with an
# on-disk fallback so it survives Egon restarts.
def _instapaper_items() -> list[dict]:
    from lib.adapters import instapaper
    return instapaper.items(5000)


def _ip_copy(text: str) -> None:
    QGuiApplication.clipboard().setText(text)


def _ip_open_urls(rows: list[dict]) -> None:
    for r in rows[:40]:
        u = r.get("url")
        if u:
            webbrowser.open(u, new=2)


# ── providers ───────────────────────────────────────────────────────────────

def _letterboxd_films() -> list[dict]:
    """Films from Letterboxd, enriched with TMDB (director/cast/genre/clean
    poster) when a TMDB key is configured. TMDB results are loaded from disk
    cache. We do NOT run live TMDB API queries in the UI path to avoid hangs."""
    from lib.adapters import letterboxd
    try:
        from lib.adapters import tmdb
        tmdb_on = tmdb.configured()
    except Exception:
        tmdb, tmdb_on = None, False

    out = []
    # Load the TMDB cache once outside the loop for speed
    cache = tmdb._load_cache() if tmdb_on else {}

    for f in letterboxd.items(5000):   # full library (was 500)
        title, year = f.get("title", ""), f.get("year", "")
        poster = f.get("poster", "")
        meta = []
        extra = {}
        if tmdb_on:
            ckey = f"{title.strip().lower()}|{str(year)[:4]}"
            extra = cache.get(ckey) or {}
            if extra.get("poster"):
                poster = extra["poster"]            # TMDB poster is cleaner/portrait
            if extra.get("director"):
                meta.append(f"dir: {extra['director']}")
            if extra.get("genres"):
                meta.append(extra["genres"].split(",")[0].strip())
            if extra.get("language"):
                meta.append(extra["language"])
        # general ranking = TMDB community rating; my rating = Letterboxd stars
        tmdb_rating = extra.get("tmdb_rating") or ""
        if tmdb_rating:
            meta.append(f"TMDB {tmdb_rating}")
        card = {
            "title":  title,
            "year":   year,
            "rating": f.get("rating"),               # MY rating (stars)
            "liked":  f.get("liked"),
            "poster": poster,
            "url":    f.get("url", ""),
            "meta":   meta,
            # rich fields — surfaced in cards + used for sort/filter
            "watched_date": f.get("watched_date", "") or f.get("date", ""),
            "tmdb_rating":  tmdb_rating,
            "director":     extra.get("director", ""),
            "cast":         extra.get("cast", ""),
            "genres":       extra.get("genres", ""),
            "language":     extra.get("language", ""),
            "country":      extra.get("country", ""),
            "runtime":      extra.get("runtime", ""),
            "overview":     extra.get("overview", ""),
        }
        out.append(card)
    return out


def _yt_card(it: dict) -> dict:
    """Map a YouTube liked-item to a poster card."""
    pub = (it.get("published") or "")[:4]
    views = it.get("views")
    meta = []
    if views:
        meta.append(f"{views:,} views")
    if it.get("duration"):
        # ISO8601 PT#M#S → compact
        d = it["duration"].replace("PT", "").replace("H", "h ").replace("M", "m ").replace("S", "s")
        meta.append(d)
    return {
        "title":    it.get("title", ""),
        "subtitle": it.get("channel", ""),
        "year":     pub,
        "image":    it.get("thumbnail", ""),
        "url":      it.get("url", ""),
        "meta":     meta,
    }


def _youtube_videos() -> list[dict]:
    """Liked YouTube videos (non-music)."""
    try:
        from lib.adapters import youtube
        return [_yt_card(v) for v in youtube.videos(100000)]
    except Exception:
        return []


def _youtube_music() -> list[dict]:
    """Liked YouTube Music tracks (Music category / '- Topic' channels)."""
    try:
        from lib.adapters import youtube
        out = []
        for v in youtube.music(100000):
            card = _yt_card(v)
            # For music, the channel is usually "<artist> - Topic"; clean it.
            card["subtitle"] = card["subtitle"].replace(" - Topic", "")
            out.append(card)
        return out
    except Exception:
        return []


def _pocketcasts_subs() -> list[dict]:
    """Subscribed podcasts with cover art."""
    try:
        from lib.adapters import pocketcasts
        return pocketcasts.podcasts()
    except Exception:
        return []


def _pocketcasts_history() -> list[dict]:
    """Recently played episodes."""
    try:
        from lib.adapters import pocketcasts
        return pocketcasts.history(500)
    except Exception:
        return []


def _kindle_books() -> list[dict]:
    """Kindle books with cover art."""
    try:
        from lib.adapters import kindle
        out = []
        for b in kindle.items(5000):
            asin = b.get("asin") or ""
            url = f"https://www.amazon.com/dp/{asin}" if asin and not str(asin).startswith("pdoc_") else ""
            out.append({
                "title":  b.get("title", ""),
                "subtitle": b.get("author", ""),
                "year":   b.get("acquired", "")[:4] if b.get("acquired") else "",
                "image":  b.get("cover") or "",
                "url":    url,
                "meta":   [b.get("kind", "")] if b.get("kind") else [],
            })
        return out
    except Exception:
        return []


def _tvtime_shows() -> list[dict]:
    """TV Time shows — from the local adapter snapshot first, falling back
    to the Chrome extension harvest endpoint on Panop."""
    try:
        from lib.adapters import tmdb
        tmdb_cache = tmdb._load_cache() if tmdb.configured() else {}
    except Exception:
        tmdb_cache = {}

    try:
        from lib.adapters import tvtime
        local_items = tvtime.items(500)
        if local_items:
            out = []
            for s in local_items:
                poster = s.get("poster") or s.get("image") or ""
                tvdb_id = s.get("tvdb_id") or ""
                if not tvdb_id and s.get("id"):
                    parts = s["id"].split("-")
                    if len(parts) > 1:
                        tvdb_id = parts[-1]
                    else:
                        parts = s["id"].split(":")
                        if len(parts) > 1:
                            tvdb_id = parts[-1]
                if tvdb_id and f"tvdb|{tvdb_id}" in tmdb_cache:
                    poster = tmdb_cache[f"tvdb|{tvdb_id}"].get("poster") or poster

                meta = []
                eps = s.get("watched_episodes")
                if eps:
                    meta.append(f"{eps} eps")
                last_w = s.get("last_watched")
                if last_w:
                    meta.append(f"watched {last_w}")
                rating = s.get("rating")
                if rating:
                    meta.append(f"rating {rating}")

                out.append({
                    "title":  s.get("title", ""),
                    "year":   s.get("year", ""),
                    "image":  poster,
                    "url":    s.get("url", ""),
                    "subtitle": s.get("status", ""),
                    "meta":   meta,
                })
            return out
    except Exception:
        pass

    # Fallback to extension harvest
    import httpx
    try:
        r = httpx.get("http://127.0.0.1:8000/api/v1/tvtime/library", timeout=2.0)
        d = r.json() or {}
        out = []
        for s in d.get("items") or []:
            poster = s.get("poster") or s.get("image") or ""
            tvdb_id = s.get("tvdb_id") or ""
            if tvdb_id and f"tvdb|{tvdb_id}" in tmdb_cache:
                poster = tmdb_cache[f"tvdb|{tvdb_id}"].get("poster") or poster

            meta = []
            eps = s.get("watched_episodes")
            if eps:
                meta.append(f"{eps} eps")
            last_w = s.get("last_watched")
            if last_w:
                meta.append(f"watched {last_w}")
            rating = s.get("rating")
            if rating:
                meta.append(f"rating {rating}")

            out.append({
                "title":  s.get("title", ""),
                "year":   s.get("year", ""),
                "image":  poster,
                "url":    s.get("url", ""),
                "subtitle": s.get("status", ""),
                "meta":   meta,
            })
        return out
    except Exception:
        return []


def _youtube_playlists() -> list[dict]:
    """Your YouTube/YT-Music playlists as cards (cover = playlist thumbnail).
    Reads the live cache (with track contents), not the stale snapshot store."""
    try:
        from lib.adapters import youtube
        out = []
        for pl in youtube.playlists():
            n_tracks = len(pl.get("tracks") or [])
            out.append({
                "title":    pl.get("title", ""),
                "subtitle": f"{pl.get('count', n_tracks)} videos",
                "image":    pl.get("thumbnail", ""),
                "url":      pl.get("url", ""),
                "year":     (pl.get("published") or "")[:4],
                "meta":     [pl.get("description", "")[:60]] if pl.get("description") else [],
            })
        return out
    except Exception:
        return []


def _letterboxd_lists() -> list[dict]:
    """Your Letterboxd lists — read live from the export ZIP."""
    try:
        from lib.adapters import letterboxd
        out = []
        for ls in letterboxd.lists():
            out.append({
                "title":    ls.get("name", ""),
                "subtitle": f"{ls.get('count', '?')} films",
                "url":      ls.get("url", ""),
                "meta":     [ls.get("description", "")[:60]] if ls.get("description") else [],
            })
        return out
    except Exception:
        return []


def _youtube_subscriptions() -> list[dict]:
    """Channels you're subscribed to (cover = channel avatar)."""
    try:
        from lib.adapters import youtube
        out = []
        for s in youtube.subscription_items(5000):
            out.append({
                "title":    s.get("channel", ""),
                "image":    s.get("thumbnail", ""),
                "url":      f"https://www.youtube.com/channel/{s.get('channelId','')}",
                "subtitle": "channel",
            })
        return out
    except Exception:
        return []


def _parse_seen_date_to_str(val: str) -> str:
    """Parse a watched date string (e.g. 'Today', 'Yesterday', 'Monday', '26 de mai') to YYYY-MM-DD."""
    import datetime as dt
    import re
    if not val:
        return "0000-00-00"
    val_clean = str(val).strip()
    if not val_clean:
        return "0000-00-00"
        
    now = dt.datetime.now()
    if val_clean.lower() == "today":
        return now.strftime("%Y-%m-%d")
    if val_clean.lower() == "yesterday":
        yest = now - dt.timedelta(days=1)
        return yest.strftime("%Y-%m-%d")
        
    days_of_week = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    if val_clean.lower() in days_of_week:
        target_idx = days_of_week.index(val_clean.lower())
        current_idx = now.weekday()
        days_ago = (current_idx - target_idx) % 7
        if days_ago == 0:
            days_ago = 7
        target_date = now - dt.timedelta(days=days_ago)
        return target_date.strftime("%Y-%m-%d")
        
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y", "%Y-%m-%d"):
        try:
            d = dt.datetime.strptime(val_clean, fmt)
            return d.strftime("%Y-%m-%d")
        except ValueError:
            pass
            
    months = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
    months_full = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]
    months_pt = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]
    
    for i, mname in enumerate(months):
        if mname in val_clean.lower():
            day_match = re.search(r'\b\d{1,2}\b', val_clean)
            if day_match:
                day = int(day_match.group(0))
                year_match = re.search(r'\b(20\d{2})\b', val_clean)
                year = int(year_match.group(0)) if year_match else now.year
                return f"{year:04d}-{i+1:02d}-{day:02d}"
                
    for i, mname in enumerate(months_full):
        if mname in val_clean.lower():
            day_match = re.search(r'\b\d{1,2}\b', val_clean)
            if day_match:
                day = int(day_match.group(0))
                year_match = re.search(r'\b(20\d{2})\b', val_clean)
                year = int(year_match.group(0)) if year_match else now.year
                return f"{year:04d}-{i+1:02d}-{day:02d}"

    for i, mname in enumerate(months_pt):
        if mname in val_clean.lower():
            day_match = re.search(r'\b\d{1,2}\b', val_clean)
            if day_match:
                day = int(day_match.group(0))
                year_match = re.search(r'\b(20\d{2})\b', val_clean)
                year = int(year_match.group(0)) if year_match else now.year
                return f"{year:04d}-{i+1:02d}-{day:02d}"
                
    if len(val_clean) >= 10 and val_clean[4] == "-" and val_clean[7] == "-":
        return val_clean[:10]
        
    m = re.search(r'\b(19|20)\d{2}\b', val_clean)
    if m:
        return f"{m.group(0)}-01-01"
        
    return "0000-00-00"


def _youtube_history() -> list[dict]:
    """Full watch history — NOT available via the YouTube Data API (Google
    removed it in 2016). Comes from the Chrome extension harvesting
    youtube.com/feed/history. Empty until that harvest runs."""
    import httpx
    try:
        r = httpx.get("http://127.0.0.1:8000/api/v1/youtube/history", timeout=2.0)
        d = r.json() or {}
        out = []
        for v in d.get("items") or []:
            watched_str = v.get("watched") or ""
            out.append({
                "title": v.get("title", ""),
                "subtitle": v.get("channel", ""),
                "image": v.get("thumbnail", ""),
                "url": v.get("url", ""),
                "year": watched_str,
                "watched_date_sort": _parse_seen_date_to_str(watched_str),
            })
        return out
    except Exception:
        return []


# ── per-platform stats lines ────────────────────────────────────────────────

def _films_stats(rows: list[dict]) -> str:
    if not rows:
        return ""
    rated = [float(r["rating"]) for r in rows if r.get("rating")]
    liked = sum(1 for r in rows if r.get("liked"))
    years = [r["year"] for r in rows if r.get("year")]
    parts = [f"<b style='color:#f5f5f7;'>{len(rows):,}</b> films"]
    if rated:
        parts.append(f"avg ★ {sum(rated)/len(rated):.1f}")
    if liked:
        parts.append(f"<span style='color:#ff453a;'>♥ {liked}</span>")
    if years:
        parts.append(f"{min(years)}–{max(years)}")
    return "  ·  ".join(parts)


def _yt_music_stats(rows: list[dict]) -> str:
    if not rows:
        return ""
    from collections import Counter
    artists = Counter(r.get("subtitle", "") for r in rows if r.get("subtitle"))
    parts = [f"<b style='color:#f5f5f7;'>{len(rows):,}</b> liked tracks"]
    top = artists.most_common(3)
    if top:
        parts.append("top: " + ", ".join(f"{a} ({n})" for a, n in top if a))
    return "  ·  ".join(parts)


def _yt_video_stats(rows: list[dict]) -> str:
    if not rows:
        return ""
    from collections import Counter
    ch = Counter(r.get("subtitle", "") for r in rows if r.get("subtitle"))
    parts = [f"<b style='color:#f5f5f7;'>{len(rows):,}</b> liked videos"]
    top = ch.most_common(3)
    if top:
        parts.append("top channels: " + ", ".join(f"{c} ({n})" for c, n in top if c))
    return "  ·  ".join(parts)


def _pc_subs_stats(rows: list[dict]) -> str:
    return f"<b style='color:#f5f5f7;'>{len(rows):,}</b> subscribed podcasts" if rows else ""


def _open(row: dict) -> None:
    url = (row.get("url") or "").strip()
    if url.startswith("http"):
        webbrowser.open(url, new=2)


class MediaPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 24, 28, 24)
        outer.setSpacing(10)

        title = QLabel("Media")
        title.setStyleSheet("font-size: 22px; font-weight: 700; color: #f5f5f7;")
        outer.addWidget(title)
        sub = QLabel("Letterboxd · YouTube · YouTube Music · Pocket Casts · TV Time · "
                     "Instapaper — click any poster (or row) to open it. Filter and sort "
                     "from the toolbar.")
        sub.setStyleSheet("color: #76767f;")
        outer.addWidget(sub)

        tabs = QTabWidget()
        tabs.setStyleSheet(
            "QTabWidget::pane { border: 1px solid #22252a; background: #0c0d0f; border-radius: 4px; }"
            "QTabBar::tab { background: #0c0d0f; color: #76767f; padding: 6px 14px; "
            "border: 1px solid #22252a; border-bottom: none; }"
            "QTabBar::tab:selected { background: #212328; color: #f5f5f7; font-weight: 600; }"
        )
        outer.addWidget(tabs, 1)

        # ── Letterboxd: films + lists in one tab ──
        tabs.addTab(SegmentedGridWidget([
            {"label": "Films", "provider": _letterboxd_films, "shape": "portrait",
             "stats_fn": _films_stats, "cache_key": "lb_films",
             "sort_fields": [("watched_date", "Recently logged"), ("rating", "My rating"),
                             ("tmdb_rating", "General ranking"), ("year", "Year"),
                             ("title", "Title"), ("genres", "Genre"), ("language", "Language")],
             "empty_message": ("No Letterboxd films. Drop your export ZIP at "
                               "letterboxd.export_path for the full library.")},
            {"label": "Lists", "provider": _letterboxd_lists, "shape": "landscape",
             "cache_key": "lb_lists", "sort_fields": [("title", "Title")],
             "empty_message": "No Letterboxd lists in the export."},
        ], on_click=_open), "Letterboxd")

        # ── YouTube: everything non-music in ONE tab (Bruno's consolidation) ──
        tabs.addTab(SegmentedGridWidget([
            {"label": "Liked videos", "provider": _youtube_videos, "shape": "landscape",
             "stats_fn": _yt_video_stats, "cache_key": "yt_liked",
             "sort_fields": [("year", "Year"), ("subtitle", "Channel"),
                             ("views", "Views"), ("title", "Title")],
             "empty_message": ("No YouTube videos yet. Authorize YouTube in Settings. "
                               "First load fetches your likes live (~10-30s).")},
            {"label": "Watch history", "provider": _youtube_history, "shape": "landscape",
             "cache_key": "yt_history", "sort_fields": [("watched_date_sort", "Seen Date"), ("year", "Year"), ("title", "Title")],
             "empty_message": ("Watch history isn't in the YouTube API (Google removed "
                               "it in 2016). The Chrome extension harvests it from "
                               "youtube.com/feed/history — open that page once.")},
            {"label": "Playlists", "provider": _youtube_playlists, "shape": "landscape",
             "cache_key": "yt_playlists", "sort_fields": [("title", "Title")],
             "empty_message": "No playlists found."},
            {"label": "Subscriptions", "provider": _youtube_subscriptions, "shape": "square",
             "cache_key": "yt_subs", "sort_fields": [("title", "Channel")],
             "empty_message": "No subscriptions found."},
        ], on_click=_open), "YouTube")

        # ── YouTube Music: tracks + music playlists ──
        tabs.addTab(SegmentedGridWidget([
            {"label": "Tracks", "provider": _youtube_music, "shape": "landscape",
             "stats_fn": _yt_music_stats, "cache_key": "ytm_tracks",
             "sort_fields": [("subtitle", "Artist"), ("year", "Year"),
                             ("views", "Plays"), ("title", "Title")],
             "empty_message": ("No YouTube Music tracks yet. Music = liked tracks in the "
                               "Music category or from '- Topic' artist channels.")},
            {"label": "Playlists", "provider": _youtube_playlists, "shape": "landscape",
             "cache_key": "yt_playlists", "sort_fields": [("title", "Title")],
             "empty_message": "No playlists found."},
        ], on_click=_open), "YouTube Music")

        # ── Pocket Casts: subscribed + history ──
        tabs.addTab(SegmentedGridWidget([
            {"label": "Subscribed", "provider": _pocketcasts_subs, "shape": "square",
             "stats_fn": _pc_subs_stats, "cache_key": "pc_subs",
             "sort_fields": [("last_published", "Recently active"), ("title", "Title"),
                             ("subtitle", "Author")],
             "empty_message": ("No Pocket Casts data yet. Set pocketcasts.email + "
                               "pocketcasts.password in Settings → Pocket Casts.")},
            {"label": "History", "provider": _pocketcasts_history, "shape": "square",
             "cache_key": "pc_history",
             "sort_fields": [("year", "Recently played"), ("title", "Title"), ("subtitle", "Podcast")],
             "empty_message": "No Pocket Casts listening history yet."},
        ], on_click=_open), "Pocket Casts")

        tabs.addTab(PosterGridWidget(
            provider=_tvtime_shows,
            on_click=_open,
            shape="portrait",
            cache_key="tvtime",
            sort_fields=[("title", "Title"), ("year", "Year")],
            empty_message="No TV Time data yet. Log in via Settings → TV Time.",
        ), "TV Time")

        tabs.addTab(PosterGridWidget(
            provider=_kindle_books,
            on_click=_open,
            shape="portrait",
            cache_key="kindle",
            sort_fields=[("title", "Title"), ("year", "Acquisition Year")],
            empty_message="No Kindle books found. Log in via Settings → Kindle.",
        ), "Kindle")

        # ── Instapaper: saved articles as a searchable list ──
        tabs.addTab(ItemListWidget(
            provider=_instapaper_items,
            cache_key="media_instapaper",
            columns=[
                ("title",       "Title",   -1),
                ("host",        "Site",    160),
                ("time",        "Saved",   120),
                ("url",         "URL",     300),
                ("description", "Preview", -1),
            ],
            actions=[
                ("Open URLs",   _ip_open_urls),
                ("Copy titles", lambda rows: _ip_copy("\n".join(r.get("title", "") for r in rows))),
                ("Copy URLs",   lambda rows: _ip_copy("\n".join(r.get("url", "") for r in rows))),
            ],
            empty_message=("No Instapaper harvest yet. Open www.instapaper.com/u "
                           "in your real Chrome — the Egon extension v1.3.2+ "
                           "will walk the paginated list automatically."),
        ), "Instapaper")
