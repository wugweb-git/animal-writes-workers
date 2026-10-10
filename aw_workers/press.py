"""Press discovery: coverage of Animal Writes on the web and social platforms.

Weekly, through Google: Google News (press_gnews) and Google Search via Gemini's search tool (press_gemini), the last
two weeks each time (owner, 10 Oct 2026: Brave has no free plan, so no Brave key). The notes below describe the
original Brave design; the platform, format and handle helpers are still used.


One source only (owner's decision, 8 Oct 2026: Brave instead of Google): web search, each query once per site filter
(instagram.com, facebook.com, fb.watch, x.com, twitter.com, youtube.com, threads.net, linkedin.com, reddit.com) plus
the open web. Weekly; at most 900 requests a month (owner's budget, S4.2) and a one-time backfill of up to 300;
queries rotate across runs; usage is logged. Every candidate that mentions
Animal Writes or the 29 November letter campaign is sent to the site as a "Press: to review" draft; nothing is
published automatically, duplicates are refused by URL (rejected items too), and no caption text is copied.

Per candidate: platform, format (Article, Video, TV, Podcast, Instagram post, Instagram reel, Facebook post, Facebook
video, X post, YouTube video, Reddit thread) and account handle. Metadata: YouTube and X oEmbed (no key), Reddit's own
.json, Instagram/Facebook via Meta oEmbed only when META_ACCESS_TOKEN is set (else the platform icon); news sites give
og and JSON-LD tags, which the site reads. The site hosts thumbnails and refuses duplicates of Press and News items.

Off until BRAVE_SEARCH_KEY exists: the job logs "disabled: no credentials" and the run carries on.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import requests

log = logging.getLogger("aw_workers.press")

API = "https://api.search.brave.com/res/v1/web/search"
STATE_FILE = Path(os.environ.get("AW_PRESS_STATE", ".state/press.json"))
TIMEOUT = 30
UA = "AnimalWritesWorkers/1.0 (+https://animalwrites.in)"

PLATFORMS = {
    "instagram.com": "instagram", "facebook.com": "facebook", "fb.watch": "facebook", "x.com": "x", "twitter.com": "x",
    "youtube.com": "youtube", "youtu.be": "youtube", "threads.net": "threads", "linkedin.com": "linkedin", "reddit.com": "reddit",
}
RELEVANT = re.compile(r"animal\s?writes|#animalwrites|letter petition|handwritten letters?|29 november|post office letters?", re.I)


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state))


def normalise(url: str) -> str:
    """One URL per item: https, no tracking parameters, Facebook on www.facebook.com, no trailing slash noise."""
    p = urlparse(url.strip())
    host = p.netloc.lower()
    if host in ("m.facebook.com", "mbasic.facebook.com", "facebook.com", "web.facebook.com"):
        host = "www.facebook.com"
    if host in ("mobile.twitter.com", "twitter.com", "www.twitter.com"):
        host = "x.com"
    query = {k: v for k, v in parse_qs(p.query).items() if not k.lower().startswith(("utm_", "fbclid", "igshid", "si"))}
    if "youtube.com" in host and "v" in parse_qs(p.query):
        query = {"v": parse_qs(p.query)["v"]}
    return urlunparse(("https", host, p.path.rstrip("/") or "/", "", urlencode(query, doseq=True), ""))


def platform_of(url: str) -> str:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    for h, name in PLATFORMS.items():
        if host == h or host.endswith("." + h):
            return name
    return "web"


def format_of(url: str, platform: str) -> str:
    """Format label (S4.3)."""
    path = urlparse(url).path.lower()
    if platform == "instagram":
        return "Instagram reel" if "/reel" in path else "Instagram post"
    if platform == "facebook":
        return "Facebook video" if ("/videos/" in path or "/watch" in path or "fb.watch" in url or "/reel" in path) else "Facebook post"
    if platform == "x":
        return "X post"
    if platform == "youtube":
        return "YouTube video"
    if platform == "reddit":
        return "Reddit thread"
    if re.search(r"/(podcasts?|episode)s?/", path):
        return "Podcast"
    if re.search(r"/(live-tv|tv|programmes?|shows?)/", path):
        return "TV"
    if re.search(r"/videos?/", path):
        return "Video"
    return "Article"


def handle_of(url: str, platform: str) -> str:
    """Account handle from the URL where the platform puts it there (@name, r/sub)."""
    parts = [p for p in urlparse(url).path.split("/") if p]
    if not parts:
        return ""
    if platform in ("instagram", "threads") and parts[0] not in ("p", "reel", "reels", "tv", "stories"):
        return "@" + parts[0].lstrip("@")
    if platform == "x" and parts[0] not in ("i", "search", "hashtag"):
        return "@" + parts[0]
    if platform == "facebook" and parts[0] not in ("watch", "share", "story.php", "permalink.php", "groups", "reel"):
        return parts[0]
    if platform == "youtube" and parts[0].startswith("@"):
        return parts[0]
    if platform == "reddit" and parts[0] == "r" and len(parts) > 1:
        return "r/" + parts[1]
    if platform == "linkedin" and parts[0] in ("in", "company") and len(parts) > 1:
        return parts[1]
    return ""


def reddit_meta(url: str, get=requests.get) -> dict:
    """Title, date and thumbnail of a Reddit thread from its own .json (no key)."""
    try:
        r = get(url.rstrip("/") + ".json", headers={"User-Agent": UA}, timeout=TIMEOUT)
        post = r.json()[0]["data"]["children"][0]["data"]
        thumb = str(post.get("thumbnail") or "")
        return {"title": post.get("title", ""), "date": datetime.fromtimestamp(float(post.get("created_utc", 0)), timezone.utc).date().isoformat() if post.get("created_utc") else "",
                "image": thumb if thumb.startswith("https://") else "", "handle": "u/" + post["author"] if post.get("author") else ""}
    except Exception:
        return {}


def result_date(item: dict) -> str:
    """The page's own date from Brave (page_age, ISO), else a date written in the age text (YYYY-MM-DD)."""
    m = re.match(r"(\d{4}-\d{2}-\d{2})", str(item.get("page_age") or ""))
    if m:
        return m.group(1)
    m = re.match(r"([A-Z][a-z]+ \d{1,2}, \d{4})", str(item.get("age") or ""))
    if m:
        for fmt in ("%B %d, %Y", "%b %d, %Y"):
            try:
                return datetime.strptime(m.group(1), fmt).date().isoformat()
            except ValueError:
                continue
    return ""


def thumbnail(url: str, platform: str, item: dict, get=requests.get) -> str:
    try:
        if platform == "youtube":
            r = get("https://www.youtube.com/oembed", params={"url": url, "format": "json"}, timeout=TIMEOUT)
            if r.ok:
                return r.json().get("thumbnail_url", "")
        if platform in ("instagram", "facebook") and os.environ.get("META_ACCESS_TOKEN"):
            endpoint = "instagram_oembed" if platform == "instagram" else "oembed_post"
            r = get(f"https://graph.facebook.com/v19.0/{endpoint}", params={"url": url, "access_token": os.environ["META_ACCESS_TOKEN"], "fields": "thumbnail_url"}, timeout=TIMEOUT)
            if r.ok:
                return r.json().get("thumbnail_url", "")
        if platform == "x":
            return ""  # X oEmbed gives HTML only, no image; the card shows the platform icon
    except requests.RequestException:
        return ""
    src = str(((item.get("thumbnail") or {}).get("original")) or ((item.get("thumbnail") or {}).get("src")) or "")
    return src if src.startswith("https://") else ""


def run(cfg: dict, client, *, dry_run: bool, env=os.environ, today: date | None = None,
        news_search=None, web_search=None) -> dict:
    """Weekly: the last two weeks of coverage, from Google News and from Google Search (via Gemini's search tool).
    Every hit goes to the site as a "Press: to review" draft. Returns counts for the log."""
    from datetime import timedelta

    from . import press_gemini, press_gnews

    if not cfg or not cfg.get("enabled", True):
        return {"status": "off"}
    state = load_state()
    today = today or datetime.now(timezone.utc).date()
    if state.get("last_run") and (today - date.fromisoformat(state["last_run"])).days < int(cfg.get("every_days", 7)):
        return {"status": "ran this week"}
    since = max(today - timedelta(days=14), date.fromisoformat(str(cfg.get("date_floor", "2025-11-01"))))
    news_search = news_search or press_gnews.collect
    web_search = web_search or press_gemini.collect
    counts: dict = {}
    for part in (
        lambda: news_search(client, dry_run, cfg, since, [(since, today + timedelta(days=1))]),
        lambda: web_search(client, dry_run, since.isoformat(), ["in the last two weeks"],
                           press_gemini.web_keys(env)),
    ):
        try:
            got, _rows = part()
        except Exception as exc:  # one failing search must not stop the other
            log.error("press: search failed: %s", exc)
            continue
        for k, v in got.items():
            counts[k] = counts.get(k, 0) + v
    state["last_run"] = today.isoformat()
    if not dry_run:
        save_state(state)
    print(f"press discovery (Google News + Google Search, since {since}): {counts}")
    return {"status": "ran", **counts}
