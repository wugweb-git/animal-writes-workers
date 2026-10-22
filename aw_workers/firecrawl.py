"""Firecrawl (owner, 22 Oct 2026): the article's own words when the plain page read gives too little, so a summary
does not come out short because the page hides its text from simple fetches (Times of India, Indian Express ...).

The key is the Firecrawl row the owner saved on the site (Settings → AI models, a key starting fc-), or the
FIRECRAWL_API_KEY secret. One scrape costs one credit and the free plan has 1,000 a month, so a run uses at most
FIRECRAWL_MAX_PER_RUN (default 5) and stops for the rest of the run once Firecrawl says the credits or the rate
limit are used up. Without a key, page_text() returns '' and nothing changes."""

from __future__ import annotations

import logging
import os
import re

import requests

log = logging.getLogger(__name__)

URL = "https://api.firecrawl.dev/v2/scrape"
_state: dict = {}


def site_key(env=os.environ, session=None) -> str:
    """The Firecrawl key saved on the site ('' when there is none or the site can't be reached)."""
    base, user, pw = env.get("WP_BASE_URL"), env.get("WP_INGEST_USER"), env.get("WP_INGEST_APP_PASSWORD")
    if not (base and user and pw):
        return ""
    try:
        resp = (session or requests).get(base.rstrip("/") + "/wp-json/aw/v1/ai-keys", auth=(user, pw), timeout=30)
        resp.raise_for_status()
        tools = resp.json().get("tools") or []
    except Exception as exc:  # noqa: BLE001
        log.info("firecrawl: site key not read: %s", type(exc).__name__)
        return ""
    return next((str(t.get("key") or "").strip() for t in tools if t.get("provider") == "firecrawl" and t.get("key")), "")


def _key() -> str:
    if "key" not in _state:
        _state["key"] = (os.environ.get("FIRECRAWL_API_KEY") or "").strip() or site_key()
        _state["used"], _state["stopped"] = 0, False
    return _state["key"]


def article_text(markdown: str, limit: int = 4000) -> str:
    """The article's paragraphs from Firecrawl's markdown: whole sentences of 20+ words, links reduced to their words;
    menus, link lists and captions are left out."""
    parts = []
    for line in (markdown or "").splitlines():
        t = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", line)
        t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)
        t = re.sub(r"[*_`>#|]+", " ", t)
        t = re.sub(r"\s+", " ", t).strip(" -")
        if len(t.split()) >= 20 and re.search(r"[.!?\"”’']$", t):
            parts.append(t)
    return " ".join(dict.fromkeys(parts))[:limit]


def page_text(url: str, session=None, limit: int = 4000) -> str:
    """The article's own words through Firecrawl ('' without a key, after the run's limit, or on any error)."""
    key = _key()
    if not key or _state["stopped"] or _state["used"] >= int(os.environ.get("FIRECRAWL_MAX_PER_RUN") or 5):
        return ""
    _state["used"] += 1
    try:
        resp = (session or requests).post(URL, headers={"Authorization": f"Bearer {key}"}, timeout=90,
                                          json={"url": url, "formats": ["markdown"], "onlyMainContent": True})
        if resp.status_code in (401, 402, 429):
            _state["stopped"] = True
            log.warning("firecrawl: HTTP %s, not used for the rest of this run", resp.status_code)
            return ""
        resp.raise_for_status()
        text = article_text(((resp.json() or {}).get("data") or {}).get("markdown") or "", limit)
    except Exception as exc:  # noqa: BLE001
        log.info("firecrawl: %s not read: %s", url, type(exc).__name__)
        return ""
    log.info("firecrawl: %s gave %d words", url, len(text.split()))
    return text
