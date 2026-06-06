"""Reusable rich card renderers — used across Media, Inbox, References, etc.

Two density modes:
- list  : table-like row (compact, scannable)
- grid  : poster-card with title overlay, rating stars, like icon

Per-source renderers map the heterogeneous item shapes to a uniform CardProps
dict. New sources only need a mapper (15 lines) to get full grid + list support.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from html import escape


@dataclass
class CardProps:
    title:    str = ""
    subtitle: str = ""
    image:    str | None = None          # poster/cover URL
    rating:   float | None = None        # 0–5
    liked:    bool = False
    badge:    str | None = None          # year, count, etc.
    url:      str | None = None          # click-through
    chips:    list[tuple[str, str]] = field(default_factory=list)  # (text, kind)


# -- per-source mappers -----------------------------------------------------

def map_letterboxd(item: dict) -> CardProps:
    poster = item.get("poster") or ""
    try:
        from lib.adapters import tmdb
        if tmdb.configured():
            t_cache = tmdb._load_cache()
            title = item.get("title", "")
            year = item.get("year", "")
            ckey = f"{title.strip().lower()}|{str(year)[:4]}"
            cached_val = t_cache.get(ckey)
            if cached_val and cached_val.get("poster"):
                poster = cached_val["poster"]
    except Exception:
        pass

    return CardProps(
        title=item.get("title") or "(untitled)",
        subtitle=item.get("watched_date") or "",
        image=poster,
        rating=float(item["rating"]) if item.get("rating") is not None else None,
        liked=bool(item.get("liked")),
        badge=str(item.get("year") or ""),
        url=item.get("url"),
    )


def map_chrome_bookmark(item: dict) -> CardProps:
    return CardProps(
        title=item.get("title") or item.get("url", "")[:80],
        subtitle=item.get("folder") or "",
        url=item.get("url"),
        chips=[(item.get("folder", "")[:20], "")] if item.get("folder") else [],
    )


def map_zotero(item: dict) -> CardProps:
    return CardProps(
        title=item.get("title") or "(untitled)",
        subtitle=item.get("doi") or "",
        chips=[("ref", "sug")] if item.get("doi") else [],
    )


def map_instapaper(item: dict) -> CardProps:
    return CardProps(
        title=item.get("title") or "(untitled)",
        subtitle=item.get("description", "")[:100],
        url=item.get("url"),
        liked=bool(item.get("starred")),
        chips=[("read", "sug")] if item.get("progress", 0) > 0.95
              else ([(f"{int(item.get('progress',0)*100)}%", "")] if item.get("progress") else []),
    )


def map_kindle(item: dict) -> CardProps:
    image = item.get("cover")
    if image and "no-image" in image:
        image = None
    kind = item.get("kind") or "Book"
    badge_label = kind
    if kind in ("KindlePDoc", "PersonalDocument", "Personal"):
        badge_label = "Doc"
    
    is_pdoc = kind in ("KindlePDoc", "PersonalDocument", "Personal") or (item.get("asin") or "").startswith("pdoc_")
    
    return CardProps(
        title=item.get("title") or "(untitled)",
        subtitle=item.get("author") or "",
        image=image,
        badge=badge_label,
        url=f"https://www.amazon.com/dp/{item['asin']}" if item.get("asin") and not is_pdoc else None
    )


def map_tvtime(item: dict) -> CardProps:
    return CardProps(
        title=item.get("title") or "(untitled)",
        subtitle=item.get("status") or "",
        image=item.get("poster") or "",
        badge=item.get("year") or "",
        url=item.get("url")
    )


def map_youtube_music(item: dict) -> CardProps:
    badge = None
    if item.get("watched"):
        badge = item["watched"]
    elif item.get("published"):
        badge = item["published"][:4]
        
    return CardProps(
        title=item.get("title") or "(untitled)",
        subtitle=item.get("channel") or "",
        image=item.get("thumbnail") or None,
        liked=bool(item.get("liked")),
        url=item.get("url"),
        badge=badge
    )


def map_youtube_playlist(item: dict) -> CardProps:
    return CardProps(
        title=item.get("title") or "(untitled)",
        subtitle=item.get("description") or "",
        image=item.get("thumbnail") or None,
        url=item.get("url"),
        badge=f"{item.get('count', 0)} items"
    )


def map_youtube_subscription(item: dict) -> CardProps:
    return CardProps(
        title=item.get("channel") or "(untitled)",
        subtitle="Subscription",
        image=item.get("thumbnail") or None,
        url=f"https://www.youtube.com/channel/{item.get('channelId')}" if item.get("channelId") else None
    )


def map_pocketcasts(item: dict) -> CardProps:
    return CardProps(
        title=item.get("title") or "(untitled)",
        subtitle=item.get("subtitle") or item.get("author") or "",
        image=item.get("image") or None,
        url=item.get("url"),
        badge=item.get("year") or None
    )


def map_generic(item: dict) -> CardProps:
    return CardProps(
        title=str(item.get("title") or item.get("name") or "(item)")[:80],
        subtitle=str(item.get("url") or item.get("doi") or "")[:80],
        url=item.get("url"),
    )


MAPPERS = {
    "letterboxd":           map_letterboxd,
    "chrome_bookmarks":     map_chrome_bookmark,
    "zotero":               map_zotero,
    "instapaper":           map_instapaper,
    "kindle":               map_kindle,
    "tvtime":               map_tvtime,
    "youtube_music":        map_youtube_music,
    "youtube_playlist":     map_youtube_playlist,
    "youtube_subscription": map_youtube_subscription,
    "pocketcasts":          map_pocketcasts,
}


def to_card(source: str, item: dict) -> CardProps:
    return MAPPERS.get(source, map_generic)(item)


# -- rendering helpers ------------------------------------------------------

def _rating_stars(r: float | None) -> str:
    """Render a 0–5 rating with half-star precision (Letterboxd style)."""
    if r is None:
        return ""
    full = int(r)
    half = (r - full) >= 0.5
    chars = "★" * full + ("½" if half else "") + "☆" * (5 - full - (1 if half else 0))
    return (f'<span style="color: var(--ledger, #f59e0b); font-size: 12px; '
            f'letter-spacing: 1px;">{chars}</span>')


def _heart(liked: bool) -> str:
    if liked:
        return ('<span title="Liked" style="color:var(--danger); font-size: 14px;">♥</span>')
    return ('<span title="Not liked" style="color: var(--muted-soft); font-size: 14px;">♡</span>')


def _hash_color(s: str) -> tuple[str, str]:
    """Deterministic gradient pair from a string — for placeholder cards."""
    if not s:
        return ("#27272a", "#3f3f46")
    h = sum(ord(c) for c in s)
    hue1 = h % 360
    hue2 = (hue1 + 40) % 360
    return (f"hsl({hue1}, 55%, 28%)", f"hsl({hue2}, 50%, 18%)")


def render_grid(items: list[tuple[str, dict]], cols: int = 6) -> str:
    """Return one big HTML string for a grid of source-mapped cards.

    items: list of (source_id, item_dict).
    """
    cells = []
    for source, item in items:
        c = to_card(source, item)
        c1, c2 = _hash_color(c.title)
        gradient_bg = f"background: linear-gradient(135deg, {c1} 0%, {c2} 100%);"
        placeholder_overlay = (
            f'<div style="position:absolute; inset:0; display:flex; align-items:center; '
            f'justify-content:center; padding:14px; text-align:center; '
            f'color:#fff; font-weight:600; font-size:14px; line-height:1.25; '
            f'text-shadow: 0 1px 4px rgba(0,0,0,0.6);">{escape(c.title[:60])}</div>'
        )
        # poster: image with gradient under it as fallback (broken poster → falls
        # back to gradient via onerror handler that hides the img)
        if c.image:
            bg = gradient_bg
            img_html = (
                f'<img src="{escape(c.image)}" loading="lazy" '
                f'onerror="this.style.display=\'none\';this.nextElementSibling.style.display=\'flex\';" '
                f'style="position:absolute; inset:0; width:100%; height:100%; '
                f'object-fit: cover; object-position: center;"/>'
                f'{placeholder_overlay.replace("display:flex", "display:none")}'
            )
            title_overlay = img_html
        else:
            bg = gradient_bg
            title_overlay = placeholder_overlay
        # heart corner
        heart = (f'<div style="position:absolute; top:6px; right:8px; '
                 f'text-shadow:0 1px 3px rgba(0,0,0,0.7);">'
                 f'{_heart(c.liked)}</div>') if c.liked else ''
        # year corner
        badge = (f'<div style="position:absolute; top:6px; left:8px; '
                 f'background: rgba(0,0,0,0.6); color: #fff; '
                 f'font-size: 10px; padding: 1px 6px; border-radius: 3px;">'
                 f'{escape(c.badge)}</div>') if c.badge else ''
        # rating below the poster (out-of-frame, in card chrome)
        rating_row = (f'<div style="display:flex; align-items:center; gap:6px; '
                      f'padding: 6px 4px 0; min-height: 20px;">'
                      f'{_rating_stars(c.rating)}'
                      f'{("&nbsp;" + _heart(c.liked)) if (c.liked and not c.image) else ""}'
                      f'</div>')
        # click-through wrapper
        href = f' href="{escape(c.url)}" target="_blank"' if c.url else ""
        title_below = (f'<div style="color: var(--text); font-size: 12px; line-height:1.3; '
                       f'padding: 2px 4px 0; '
                       f'overflow:hidden; text-overflow:ellipsis; display:-webkit-box; '
                       f'-webkit-line-clamp:2; -webkit-box-orient:vertical; min-height: 30px;">'
                       f'{escape(c.title[:80])}</div>')
        sub = (f'<div style="color: var(--muted); font-size: 11px; padding: 1px 4px 2px;">'
               f'{escape(c.subtitle[:60])}</div>') if c.subtitle else ''
        cells.append(f'''
        <a{href} style="text-decoration:none;" title="{escape(c.title)}">
          <div style="background: var(--surface); border-radius: 6px; overflow: hidden;
                      transition: transform 0.15s ease, box-shadow 0.15s ease;"
               onmouseover="this.style.transform='translateY(-2px)';this.style.boxShadow='0 6px 18px rgba(0,0,0,0.4)'"
               onmouseout="this.style.transform='';this.style.boxShadow=''">
            <div style="position: relative; aspect-ratio: 2/3; {bg}">
              {title_overlay}{badge}{heart}
            </div>
            {rating_row}
            {title_below}
            {sub}
          </div>
        </a>
        ''')
    return (f'<div style="display:grid; grid-template-columns: repeat({cols}, 1fr); '
            f'gap: 14px;">{"".join(cells)}</div>')


def render_list(items: list[tuple[str, dict]]) -> str:
    """Single-row-per-item list, like an Egon table."""
    rows = []
    for source, item in items:
        c = to_card(source, item)
        href = f' href="{escape(c.url)}" target="_blank"' if c.url else ""
        chips = "".join(f'<span class="chip {kind}">{escape(text)}</span>'
                        for text, kind in c.chips)
        rows.append(f'''
        <tr>
          <td style="width:36px;">{_heart(c.liked)}</td>
          <td>
            <a{href} style="color: var(--text); font-weight:500; text-decoration:none;">{escape(c.title)}</a>
            {("<div style='font-size:11px; color: var(--muted);'>" + escape(c.subtitle) + "</div>") if c.subtitle else ""}
          </td>
          <td style="width:120px;">{_rating_stars(c.rating)}</td>
          <td style="width:80px; text-align:right; color: var(--muted); font-size: 12px;">{escape(c.badge or "")}</td>
          <td>{chips}</td>
        </tr>
        ''')
    return f'''
    <div class="panel">
      <div class="pbody flush">
        <table class="stbl">
          <tbody>{"".join(rows)}</tbody>
        </table>
      </div>
    </div>
    '''
