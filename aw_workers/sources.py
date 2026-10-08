"""Fetch and parse sources into plain Item records."""

from __future__ import annotations

import html
import json
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
    source_type: str = ""
    org: str = ""  # opportunities: organisation offering them
    opportunity_type: str = ""  # opportunities: job | volunteer | internship | grant | event | ...
    lookback_days: int | None = None  # overrides the default (job pages stay open for months)
    match: str = "text"  # keywords looked for in "text" (title + feed text) or "title" only
    ministries: list[str] = field(default_factory=list)  # egazette: ministries we follow


@dataclass
class Item:
    source: Source
    title: str
    url: str
    date: date | None
    text: str = ""  # whatever context we have for the summary (feed description etc.)
    publisher: str = ""  # original publisher when the feed is an aggregator (Google News)
    image: str = ""  # preview image URL (linked, never downloaded)
    place: str = ""  # opportunities: city/state as published
    deadline: date | None = None  # opportunities: last date, when published
    doc_kind: str = ""  # Library documents: judgment | order | notification | rule | act
    court: str = ""  # judgments: supreme-court | high-court
    document_url: str = ""  # the PDF, when there is one


TOPICS: dict[str, list[str]] = {}


def topics_for(item: "Item", extra: str = "") -> list[str]:
    """Topic slugs whose keywords occur in the item's title, feed text or `extra` (e.g. our summary)."""
    hay = f" {item.title} {item.text} {extra} ".lower()
    found = []
    for slug, words in TOPICS.items():
        for w in words:
            w = w.lower()
            if (re.search(rf"\b{re.escape(w)}\b", hay) if w.isascii() and len(w) <= 4 else w in hay):
                found.append(slug)
                break
    return found


def load_sources(path: str | Path = "sources.yaml") -> tuple[list[Source], dict]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    TOPICS.clear()
    TOPICS.update({k: [str(w) for w in v] for k, v in (data.get("topics") or {}).items()})
    sources = []
    for s in data.get("sources", []):
        if s["type"] not in ("news", "legal", "opportunity"):
            raise ValueError(f"source {s['id']}: type must be news, legal or opportunity")
        if s["kind"] not in PARSERS and s["kind"] not in SELF_FETCH:
            raise ValueError(f"source {s['id']}: unknown kind {s['kind']}")
        sources.append(
            Source(
                id=s["id"],
                name=s["name"],
                kind=s["kind"],
                type=s["type"],
                url=s["url"],
                keywords=list(s.get("keywords") or []),
                source_type=s.get("source_type", ""),
                org=s.get("org", ""),
                opportunity_type=s.get("opportunity_type", ""),
                lookback_days=s.get("lookback_days"),
                match=s.get("match", "text"),
                ministries=list(s.get("ministries") or []),
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
        publisher = _clean((e.get("source") or {}).get("title", ""))
        if publisher and title.endswith(f" - {publisher}"):
            title = title[: -len(publisher) - 3].strip()
        items.append(Item(source, title, link, when, _clean(e.get("summary", "")), publisher))
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


_DEADLINE = re.compile(r"(?:last date|deadline|apply by|on or before)[^0-9]{0,20}(\d{1,2})[./-](\d{1,2})[./-](\d{4})", re.I)


_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_DEADLINE_WORDS = re.compile(r"(?:last date|deadline|apply by|on or before)[^0-9]{0,30}(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})", re.I)


def deadline_in(text: str) -> date | None:
    """A last date written in the text: 'Last date to apply - 31.10.2026' or 'Last date for application - 5 September 2026'."""
    m = _DEADLINE.search(text or "")
    try:
        if m:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        w = _DEADLINE_WORDS.search(text or "")
        if w and w.group(2)[:3].lower() in _MONTHS:
            return date(int(w.group(3)), _MONTHS[w.group(2)[:3].lower()], int(w.group(1)))
    except ValueError:
        return None
    return None


def parse_zoho(source: Source, body: str) -> list[Item]:
    """Zoho Recruit career site: the open jobs are embedded as JSON (HTML-escaped) in a hidden input."""
    text = html.unescape(body)
    start = text.find('[{"', max(0, text.find('"Posting_Title":"') - 200000))
    items = []
    while start != -1:
        depth, end = 0, -1
        for k in range(start, len(text)):
            if text[k] == "[":
                depth += 1
            elif text[k] == "]":
                depth -= 1
                if depth == 0:
                    end = k + 1
                    break
        try:
            jobs = json.loads(text[start:end]) if end > 0 else []
        except ValueError:
            jobs = []
        if jobs and isinstance(jobs[0], dict) and "Posting_Title" in jobs[0]:
            base = source.url.split("/jobs/")[0] + "/jobs/Careers/"
            for j in jobs:
                if not j.get("Publish", True) or not j.get("Posting_Title"):
                    continue
                when = None
                if j.get("Date_Opened"):
                    try:
                        when = date.fromisoformat(j["Date_Opened"][:10])
                    except ValueError:
                        when = None
                place = ", ".join(x for x in (j.get("City"), j.get("State")) if x)
                desc = _clean(j.get("Job_Description") or "")
                items.append(Item(source, _clean(j["Posting_Title"]), base + str(j["id"]), when, desc[:1500],
                                  place=place, deadline=deadline_in(desc)))
            return items
        start = text.find('[{"', start + 2)
    return items


def parse_sjm(source: Source, body: str) -> list[Item]:
    """WordPress 'Simple Job Manager' accordion (e.g. Wildlife SOS careers): one .sjm-item per role."""
    soup = BeautifulSoup(body, "html.parser")
    items = []
    for it in soup.select(".sjm-accordion .sjm-item"):
        title_el = it.select_one(".sjm-title")
        if not title_el:
            continue
        title = _clean(title_el.get_text(" "))
        place_el = it.select_one(".sjm-location")
        body_el = it.select_one(".sjm-content")
        text = _clean(body_el.get_text(" ")) if body_el else ""
        anchor = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        items.append(Item(source, title, f"{source.url.split('#')[0]}#{anchor}", None, text[:1500],
                          place=_clean(place_el.get_text(" ")) if place_el else "", deadline=deadline_in(text)))
    return items


PARSERS = {"rss": parse_rss, "awbi": parse_awbi, "zoho": parse_zoho, "sjm": parse_sjm}


def matches_keywords(item: Item, keywords: list[str]) -> bool:
    if not keywords:
        return True
    hay = (item.title if item.source.match == "title" else f"{item.title} {item.text}").lower()
    return any(k.lower() in hay for k in keywords)


def collect(source: Source, body: str, today: date, lookback_days: int, limit: int) -> list[Item]:
    parser = SELF_FETCH.get(source.kind) or PARSERS[source.kind]
    items = [i for i in parser(source, body) if matches_keywords(i, source.keywords)]
    if source.lookback_days is not None:
        lookback_days = int(source.lookback_days)
    if source.type == "opportunity":
        for i in items:
            i.deadline = i.deadline or deadline_in(i.title)
        items = [i for i in items if not i.deadline or i.deadline >= today]  # closed calls are skipped
    fresh = []
    for i in items:
        if i.date is None:
            i.date = today  # feed gave no date (e.g. PIB): use the day we found it
        if (today - i.date).days <= lookback_days:
            fresh.append(i)
    return fresh[:limit]


def utc_today() -> date:
    return datetime.now(timezone.utc).date()


# Sources that fetch their own data (several requests, binary files); run.py does not fetch their url first.
from . import documents as _documents  # noqa: E402  (needs Item and Source above)

SELF_FETCH = {  # looked up when called, so either module can be imported first
    "egazette": lambda source, body: _documents.parse_egazette(source, body),
    "judgments-sc": lambda source, body: _documents.parse_judgments_sc(source, body),
    "judgments-hc": lambda source, body: _documents.parse_judgments_hc(source, body),
}
