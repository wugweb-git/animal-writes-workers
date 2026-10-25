"""Firecrawl (owner, 22 Oct 2026): the article's own words when the plain page read gives too little, so a summary
does not come out short because the page hides its text from simple fetches (Times of India, Indian Express ...).

The key is the Firecrawl row the owner saved on the site (Settings → AI models, a key starting fc-), or the
FIRECRAWL_API_KEY secret. One scrape costs one credit and the free plan has 1,000 a month, so a run uses at most
FIRECRAWL_MAX_PER_RUN (default 5) and stops for the rest of the run once Firecrawl says the credits or the rate
limit are used up. Without a key, page_text() returns '' and nothing changes."""

from __future__ import annotations

import logging
import math
import os
import re
from datetime import datetime, timezone

import requests

from .worker_id import ai_keys_params

log = logging.getLogger(__name__)

URL = "https://api.firecrawl.dev/v2/scrape"
CREDITS_URL = "https://api.firecrawl.dev/v2/team/credit-usage"
_state: dict = {}


def site_key(env=os.environ, session=None) -> str:
    """The Firecrawl key saved on the site ('' when there is none or the site can't be reached)."""
    base, user, pw = env.get("WP_BASE_URL"), env.get("WP_INGEST_USER"), env.get("WP_INGEST_APP_PASSWORD")
    if not (base and user and pw):
        return ""
    try:
        resp = (session or requests).get(base.rstrip("/") + "/wp-json/aw/v1/ai-keys", params=ai_keys_params(env),
                                             auth=(user, pw), timeout=30)
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


def credits(session=None) -> dict | None:
    """{remaining, period_end (datetime)} of the Firecrawl plan, or None (no key, or not read)."""
    key = _key()
    if not key:
        return None
    try:
        resp = (session or requests).get(CREDITS_URL, headers={"Authorization": f"Bearer {key}"}, timeout=20)
        resp.raise_for_status()
        data = (resp.json() or {}).get("data") or {}
        end = datetime.fromisoformat(str(data["billingPeriodEnd"]).replace("Z", "+00:00"))
        return {"remaining": int(data["remainingCredits"]), "period_end": end}
    except Exception as exc:  # noqa: BLE001
        log.info("firecrawl: credits not read: %s", type(exc).__name__)
        return None


def run_share(info: dict | None, per_day_kept: int, runs_per_day: int, cap: int, now: datetime | None = None) -> int:
    """Pages a run of a background worker may read through Firecrawl (owner, 25 Oct 2026: "utilise firecrawl key too",
    within the free credits). `per_day_kept` credits a day stay for sourcing until the plan renews; the rest is shared
    over the days left and the runs of a day, at least 1 a run while that rest lasts; never more than `cap`. The
    remaining credits are read at each run's start, so a run that spent more leaves less for the next. Unknown: 0."""
    if not info:
        return 0
    now = now or datetime.now(timezone.utc)
    days = max(1, math.ceil((info["period_end"] - now).total_seconds() / 86400))
    spare = info["remaining"] - per_day_kept * days
    if spare <= 0:
        return 0
    return max(1, min(cap, spare // (days * max(1, runs_per_day))))


def set_run_cap(n: int) -> None:
    """Pages this run may read through Firecrawl (instead of FIRECRAWL_MAX_PER_RUN)."""
    _key()
    _state["cap"] = max(0, int(n))


def page_text(url: str, session=None, limit: int = 4000) -> str:
    """The article's own words through Firecrawl ('' without a key, after the run's limit, or on any error)."""
    key = _key()
    cap = _state["cap"] if "cap" in _state else int(os.environ.get("FIRECRAWL_MAX_PER_RUN") or 5)
    if not key or _state["stopped"] or _state["used"] >= cap:
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
