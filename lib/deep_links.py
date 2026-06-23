"""Native deep links — open a referenced entry in its phone app, not the browser.

Bruno 2026-06-23: when Egon Connect surfaces a hit on the phone, tapping it
used to just open the web URL in the mobile browser. For sources that have a
real Android app installed (Notion, Google Drive/Docs, YouTube, Spotify,
Letterboxd, Instapaper, Pocket Casts…) we'd rather hand the entry straight to
that app — open the Drive file in Drive, the Notion note in Notion, and so on.

The robust Android mechanism for "open in app, else fall back to web" is an
`intent://` URL carrying the app package and an `S.browser_fallback_url`. If
the app is installed the OS routes the link to it; if not, Chrome follows the
fallback. We compute that server-side (host + source give us the package) and
attach it to each connection as `app_url`; the phone UI uses it on Android and
plain `url` everywhere else (desktop widget, iOS), so nothing breaks off-device.

This module has no external deps and never raises — a bad/unknown URL just
yields no app link, and the caller keeps the web URL.
"""
from __future__ import annotations

from urllib.parse import quote, urlsplit

# host suffix  ->  (Android package, human label). Matched against the URL's
# hostname by suffix so subdomains (docs.google.com, m.youtube.com) resolve too.
_HOST_APP: list[tuple[str, str, str]] = [
    ("notion.so",            "notion.id",                                "Notion"),
    ("notion.site",          "notion.id",                                "Notion"),
    ("docs.google.com",      "com.google.android.apps.docs.editors.docs", "Google Docs"),
    ("sheets.google.com",    "com.google.android.apps.docs.editors.sheets", "Google Sheets"),
    ("slides.google.com",    "com.google.android.apps.docs.editors.slides", "Google Slides"),
    ("drive.google.com",     "com.google.android.apps.docs",             "Google Drive"),
    ("music.youtube.com",    "com.google.android.apps.youtube.music",    "YouTube Music"),
    ("youtube.com",          "com.google.android.youtube",               "YouTube"),
    ("youtu.be",             "com.google.android.youtube",               "YouTube"),
    ("open.spotify.com",     "com.spotify.music",                        "Spotify"),
    ("letterboxd.com",       "com.letterboxd.android",                   "Letterboxd"),
    ("instapaper.com",       "com.instapaper.android",                   "Instapaper"),
    ("getpocket.com",        "com.ideashower.readitlater.pro",           "Pocket"),
    ("pca.st",               "au.com.shiftyjelly.pocketcasts",           "Pocket Casts"),
    ("pocketcasts.com",      "au.com.shiftyjelly.pocketcasts",           "Pocket Casts"),
    ("twitter.com",          "com.twitter.android",                      "X"),
    ("x.com",                "com.twitter.android",                      "X"),
    ("goodreads.com",        "com.goodreads",                            "Goodreads"),
    ("read.amazon.com",      "com.amazon.kindle",                        "Kindle"),
]


def _intent_url(url: str, package: str) -> str | None:
    """Build an Android `intent://` link that opens `url` in `package`, falling
    back to the original web URL if the app isn't installed. Only http(s) URLs
    are eligible; anything else keeps the plain web link."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    # Everything after the scheme — host + path + query + fragment — becomes the
    # intent target; the original scheme is carried in the `scheme=` field.
    target = url.split("://", 1)[1]
    fallback = quote(url, safe="")
    return (f"intent://{target}#Intent;scheme={parts.scheme};"
            f"package={package};S.browser_fallback_url={fallback};end")


def native_link(url: str | None, source: str | None = None) -> dict:
    """Best native-app deep link for a connection.

    Returns {"app_url": str|None, "app_label": str|None}. `app_url` is an
    Android intent:// URL (with a web fallback baked in) when the URL's host
    belongs to an app we recognise, else None — in which case the caller keeps
    the web `url`. `source` is accepted for context but the host is the signal
    we trust (an Instapaper item's URL is the publisher's site, not the app's).
    """
    url = (url or "").strip()
    if not url:
        return {"app_url": None, "app_label": None}

    host = (urlsplit(url).hostname or "").lower().lstrip(".")
    if host.startswith("www."):
        host = host[4:]

    for suffix, pkg, lbl in _HOST_APP:
        if host == suffix or host.endswith("." + suffix):
            return {"app_url": _intent_url(url, pkg), "app_label": lbl}
    return {"app_url": None, "app_label": None}


def enrich(connections: list[dict]) -> list[dict]:
    """Attach `app_url` / `app_label` to every connection hit in place."""
    for c in connections or []:
        try:
            link = native_link(c.get("url"), c.get("source"))
        except Exception:
            link = {"app_url": None, "app_label": None}
        c["app_url"] = link["app_url"]
        c["app_label"] = link["app_label"]
    return connections
