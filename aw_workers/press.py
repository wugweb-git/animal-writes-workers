"""Press discovery: coverage of Animal Writes on the web and social platforms, found with Google Programmable Search.

One source only (owner's decision, 8 Oct 2026): the Custom Search JSON API, each query once per site filter
(instagram.com, facebook.com, fb.watch, x.com, twitter.com, youtube.com, threads.net, linkedin.com, reddit.com) plus
the open web. Queries rotate across days to stay inside the free 100 requests a day. Every candidate that mentions
Animal Writes or the 29 November letter campaign is sent to the site as a "Press: to review" draft; nothing is
published automatically, duplicates are refused by URL (rejected items too), and no caption text is copied.

Thumbnails: YouTube and X oEmbed (no key); news sites give og tags (the site reads them); Instagram/Facebook show the
platform icon unless META_ACCESS_TOKEN is set (not used yet).

Off until GOOGLE_CSE_KEY and GOOGLE_CSE_CX exist: the job logs "disabled: no credentials" and the run carries on.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import requests

log = logging.getLogger("aw_workers.press")

API = "https://www.googleapis.com/customsearch/v1"
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
    if platform == "youtube" or "/reel" in url or "/videos/" in url or "fb.watch" in url:
        return "video"
    return "article"


def result_date(item: dict) -> str:
    """The page's own date from Google's pagemap metatags, if any (YYYY-MM-DD)."""
    for tags in (item.get("pagemap") or {}).get("metatags") or []:
        for k in ("article:published_time", "og:updated_time", "date", "datepublished", "uploaddate"):
            v = str(tags.get(k) or "")
            m = re.match(r"(\d{4}-\d{2}-\d{2})", v)
            if m:
                return m.group(1)
    m = re.match(r"([A-Z][a-z]{2} \d{1,2}, \d{4})", item.get("snippet", ""))
    if m:
        try:
            return datetime.strptime(m.group(1), "%b %d, %Y").date().isoformat()
        except ValueError:
            pass
    return ""


def thumbnail(url: str, platform: str, item: dict, get=requests.get) -> str:
    try:
        if platform == "youtube":
            r = get("https://www.youtube.com/oembed", params={"url": url, "format": "json"}, timeout=TIMEOUT)
            if r.ok:
                return r.json().get("thumbnail_url", "")
        if platform == "x":
            return ""  # X oEmbed gives HTML only, no image; the card shows the platform icon
    except requests.RequestException:
        return ""
    for img in (item.get("pagemap") or {}).get("cse_image") or []:
        if str(img.get("src", "")).startswith("https://"):
            return img["src"]
    return ""


def plan(queries: list[str], sites: list[str], state: dict, budget: int) -> list[tuple[str, str]]:
    """Today's (query, site) pairs: continue where yesterday stopped, at most `budget` requests."""
    pairs = [(q, s) for q in queries for s in sites + [""]]
    start = int(state.get("next", 0)) % max(len(pairs), 1)
    today = (pairs[start:] + pairs[:start])[:budget]
    state["next"] = (start + len(today)) % max(len(pairs), 1)
    return today


def candidates(cfg: dict, key: str, cx: str, state: dict, get=requests.get) -> list[dict]:
    floor = str(cfg.get("date_floor", "2025-11-01"))
    out, seen = [], set()
    used = 0
    for query, site in plan(list(cfg.get("queries") or []), list(cfg.get("sites") or []), state, int(cfg.get("daily_requests", 90))):
        q = f"{query} site:{site}" if site else query
        resp = get(API, params={"key": key, "cx": cx, "q": q, "num": 10, "sort": "date"}, timeout=TIMEOUT, headers={"User-Agent": UA})
        used += 1
        if resp.status_code == 429:
            log.warning("press: Google quota reached after %d requests", used)
            break
        resp.raise_for_status()
        for item in resp.json().get("items") or []:
            url = normalise(item.get("link", ""))
            text = f"{item.get('title', '')} {item.get('snippet', '')}"
            if url in seen or not RELEVANT.search(text):
                continue
            when = result_date(item)
            if when and when < floor:
                continue
            seen.add(url)
            platform = platform_of(url)
            out.append({
                "type": "press",
                "title": item.get("title", "")[:200],
                "source_url": url,
                "source_name": item.get("displayLink", "").removeprefix("www."),
                "source_date": when,
                "platform": platform,
                "format": format_of(url, platform),
                "image_url": thumbnail(url, platform, item, get=get),
                "query": query,
                "feed": "press-discovery",
            })
    state["requests_today"] = used
    log.info("press: %d request(s), %d candidate(s)", used, len(out))
    return out


def run(cfg: dict, client, *, dry_run: bool, env=os.environ, get=requests.get, today: date | None = None) -> dict:
    """Once a day. Returns counts for the log."""
    key, cx = env.get("GOOGLE_CSE_KEY", ""), env.get("GOOGLE_CSE_CX", "")
    if not cfg or not cfg.get("enabled", True):
        return {"status": "off"}
    if not key or not cx:
        log.info("press: disabled: no credentials (GOOGLE_CSE_KEY, GOOGLE_CSE_CX)")
        return {"status": "disabled: no credentials"}
    state = load_state()
    today = today or datetime.now(timezone.utc).date()
    if state.get("last_run") == today.isoformat():
        return {"status": "already ran today"}
    counts = {"new": 0, "duplicate": 0, "failed": 0}
    try:
        found = candidates(cfg, key, cx, state, get=get)
        state["errors_in_a_row"] = 0
    except Exception as exc:  # one failing source must not stop the run
        state["errors_in_a_row"] = int(state.get("errors_in_a_row", 0)) + 1
        log.error("press: search failed: %s", exc)
        if state["errors_in_a_row"] >= 3:
            print(f"::warning::Press discovery has failed {state['errors_in_a_row']} runs in a row: {exc}")
        if not dry_run:
            save_state(state)
        return {"status": "error"}
    for payload in found:
        if dry_run:
            log.info("DRY_RUN would POST press: %s", json.dumps(payload, ensure_ascii=False))
            counts["new"] += 1
            continue
        try:
            result = client.post(payload)
            counts["duplicate" if result == "duplicate" else "new"] += 1
        except Exception as exc:
            counts["failed"] += 1
            log.error("press: failed %s: %s", payload["source_url"], exc)
    state["last_run"] = today.isoformat()
    if not dry_run:
        save_state(state)
    print(f"press discovery: requests={state.get('requests_today', 0)} new={counts['new']} duplicate={counts['duplicate']} failed={counts['failed']}")
    return {"status": "ran", **counts}
