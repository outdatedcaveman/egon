"""Build the AI-arbiter worklist from the FULL Chrome history, honoring Bruno's
rule: NO article/book/longform/science-news link may be missed. So everything is
sent to the AI EXCEPT (a) links already in Zotero, and (b) hard, definitional
noise that can never be a saveable KMS item (search engines, video, social,
email, banking, shopping carts, app dashboards, localhost, raw files). Content
platforms (medium, substack, blogs, news, unknown domains) ALL go to the AI.
"""
from __future__ import annotations
import json, re
from pathlib import Path
from collections import Counter
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
import requests

ROOT = Path(__file__).resolve().parents[1]
HISTORY = Path.home() / "Desktop" / "Takeout" / "Chrome" / "History.json"
OUT = ROOT / "state" / "panop" / "routster_ai_worklist.json"
TRACK = {"utm_source","utm_medium","utm_campaign","utm_term","utm_content","fbclid","gclid",
         "mc_cid","mc_eid","igshid","_ga","ref","ref_src","yclid","msclkid","spm","share","shared",
         "from","source","_hsenc","_hsmi","gad_source"}

# Hard, definitional noise — a URL here can NEVER be a saveable article/book/
# news/longform. Conservative on purpose; anything not listed goes to the AI.
NOISE_HOST_SUBSTR = (
    "google.com/search", "google.com/maps", "bing.com/search", "duckduckgo.com",
    "youtube.com", "youtu.be", "netflix.com", "twitch.tv", "spotify.com",
    "facebook.com", "instagram.com", "tiktok.com", "x.com", "twitter.com",
    "whatsapp.com", "web.whatsapp", "messenger.com", "t.me", "telegram",
    "mail.google.com", "outlook.", "calendar.google", "drive.google.com",
    "accounts.google", "login.", "signin.", "auth.", "/oauth", "myaccount.google",
    "amazon.com/gp/cart", "amazon.com/ap/", "/checkout", "/cart",
    "localhost", "127.0.0.1", "chrome://", "chrome-extension://", "about:",
    "stackoverflow.com", "github.com", "gitlab.com", "stackexchange.com",
    "linkedin.com/feed", "reddit.com/r/", "pinterest.", "ebay.", "mercadolivre",
    "booking.com", "airbnb.", "uber.com", "ifood.", "nubank", "itau", "bradesco",
    "speedtest.", "translate.google", "docs.google.com/spreadsheets",
)
NOISE_EXACT_HOSTS = {"google.com", "www.google.com", "bing.com", "gmail.com",
    "calendar.google.com", "news.google.com", "chatgpt.com", "chat.openai.com",
    "claude.ai", "gemini.google.com", "notion.so", "www.notion.so", "trello.com"}


def canon(u):
    try:
        p = urlparse(u); net = (p.netloc or "").lower()
        if net.startswith("m."): net = "www." + net[2:]
        path = (p.path or "").rstrip("/") or "/"
        qs = sorted((k, v) for k, v in parse_qsl(p.query) if k.lower() not in TRACK)
        return urlunparse(((p.scheme or "https").lower(), net, path, "", urlencode(qs), ""))
    except Exception:
        return u


def is_noise(u):
    p = urlparse(u); host = (p.netloc or "").lower()
    if host in NOISE_EXACT_HOSTS and len((p.path or "").strip("/")) == 0:
        return True
    low = u.lower()
    return any(s in low for s in NOISE_HOST_SUBSTR)


def main():
    bh = json.loads(HISTORY.read_text(encoding="utf-8")).get("Browser History") or []
    seen = {}
    for e in bh:
        u = e.get("url")
        if not u or not u.startswith("http"):
            continue
        t = (e.get("title") or "").strip()
        if u not in seen or (t and not seen[u]):
            seen[u] = t

    # existing Zotero Panop URLs (dedup)
    pe = json.loads((ROOT / "panop_env.json").read_text(encoding="utf-8-sig"))
    H = {"Zotero-API-Key": pe["zotero_api_key"], "Zotero-API-Version": "3"}
    base = f"https://api.zotero.org/users/{pe['zotero_user_id']}"
    existing = set()
    for ck in ["GKSJSJMJ", "B3XGDC4J", "BRZ3UUIR", "24A43HSI"]:
        start = 0
        while True:
            r = requests.get(f"{base}/collections/{ck}/items/top?limit=100&start={start}", headers=H, timeout=40)
            if r.status_code != 200: break
            b = r.json()
            if not b: break
            for it in b:
                uu = it.get("data", {}).get("url")
                if uu: existing.add(canon(uu))
            if len(b) < 100: break
            start += len(b)

    work, noise, already = [], 0, 0
    for u, t in seen.items():
        if canon(u) in existing:
            already += 1; continue
        if is_noise(u):
            noise += 1; continue
        work.append({"url": u, "title": t})
    print(f"unique URLs: {len(seen)}")
    print(f"  already in Zotero: {already}")
    print(f"  hard noise (skipped): {noise}")
    print(f"  -> AI worklist (could be saveable): {len(work)}")
    OUT.write_text(json.dumps(work, ensure_ascii=False), encoding="utf-8")
    print(f"worklist -> {OUT}")


if __name__ == "__main__":
    main()
