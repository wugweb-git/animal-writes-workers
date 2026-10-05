"""Fetch and parse sources into plain Item records."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup

USER_AGENT = "AnimalWritesWorkers/1.0 (+https://animalwrites.in)"
TIMEOUT = 30


@dataclass
class Source:
    id: str
    name: str
    kind: str
    type: str
    url: str
    keywords: list[str] = field(default_factory=list)


@dataclass
class Item:
    source: Source
    title: str
    url: str
    date: date | None
    text: str = ""  # whatever context we have for the summary (feed description etc.)


def load_sources(path: str | Path = "sources.yaml") -> tuple[list[Source], dict]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    sources = []
    for s in data.get("sources", []):
        if s["type"] not in ("news", "legal"):
            raise ValueError(f"source {s['id']}: type must be news or legal")
        if s["kind"] not in PARSERS:
            raise ValueError(f"source {s['id']}: unknown kind {s['kind']}")
        sources.append(
            Source(
                id=s["id"],
                name=s["name"],
                kind=s["kind"],
                type=s["type"],
                url=s["url"],
                keywords=list(s.get("keywords") or []),
            )
        )
    return sources, data.get("defaults", {})


def fetch(url: str) -> str:
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    resp.raise_for_status()
    resp.encoding = resp.encoding or "utf-8"
    return resp.text


def _clean(text: str) -> str:
    text = BeautifulSoup(html.unescape(text or ""), "html.parser").get_text(" ")
    return re.sub(r"\s+", " ", text).strip()


def parse_rss(source: Source, body: str) -> list[Item]:
    feed = feedparser.parse(body.lstrip("﻿"))
    items = []
    for e in feed.entries:
        title = _clean(e.get("title", ""))
        link = (e.get("link") or "").strip()
        if not title or not link:
            continue
        when = None
        if e.get("published"):
            try:
                when = parsedate_to_datetime(e.published).astimezone(timezone.utc).date()
            except (TypeError, ValueError):
                when = None
        items.append(Item(source, title, link, when, _clean(e.get("summary", ""))))
    return items


_DMY = re.compile(r"^(\d{2})-(\d{2})-(\d{4})$")
_UPLOAD_TS = re.compile(r"/uploads/[^/]+/(\d{10})\d*")


def _date_from_upload_name(href: str) -> date | None:
    """AWBI file names start with a Unix timestamp (e.g. 179101213684... -> 2026-10-03)."""
    m = _UPLOAD_TS.search(href)
    if not m:
        return None
    try:
        return datetime.fromtimestamp(int(m.group(1)), timezone.utc).date()
    except (OverflowError, OSError, ValueError):
        return None


def parse_awbi(source: Source, body: str) -> list[Item]:
    """AWBI table pages: each row has a DD-MM-YYYY cell and a 'downBtn' PDF link whose title is the item title."""
    soup = BeautifulSoup(body, "html.parser")
    items = []
    for a in soup.select("a.downBtn[href]"):
        title = _clean(a.get("title", ""))
        if not title:
            continue
        when = None
        row = a.find_parent("tr")
        cells = row.find_all("td") if row else []
        for td in cells:
            m = _DMY.match(td.get_text(strip=True))
            if m:
                d, mth, y = map(int, m.groups())
                when = date(y, mth, d)
                break
        if when is None:  # the Notifications page has no date column
            when = _date_from_upload_name(a["href"])
        items.append(Item(source, title, urljoin(source.url, a["href"]).replace(" ", "%20"), when))
    return items


PARSERS = {"rss": parse_rss, "awbi": parse_awbi}


def matches_keywords(item: Item, keywords: list[str]) -> bool:
    if not keywords:
        return True
    hay = f"{item.title} {item.text}".lower()
    return any(k.lower() in hay for k in keywords)


def collect(source: Source, body: str, today: date, lookback_days: int, limit: int) -> list[Item]:
    items = [i for i in PARSERS[source.kind](source, body) if matches_keywords(i, source.keywords)]
    fresh = []
    for i in items:
        if i.date is None:
            i.date = today  # feed gave no date (e.g. PIB): use the day we found it
        if (today - i.date).days <= lookback_days:
            fresh.append(i)
    return fresh[:limit]


def utc_today() -> date:
    return datetime.now(timezone.utc).date()
