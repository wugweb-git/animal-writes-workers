"""Other coverage of the same event (owner, 15 Oct 2026: "fix the summaries instead of putting them under review").

When a summary stays below its section's minimum because the source is short, thin or unreadable, the workers look
for other reports of the same event: a Google News search (the same RSS search keyword_searches() uses) with the key
names of the title and a window of days around the report's date. A result counts only when its headline shares most
of the key words of ours (the same event, not the same subject) and its page can be read. Their facts are merged into
the text the summary is written from; their publishers are credited as logos in the source row (aw/v1/same-story),
never in the text. Nothing here bypasses a block: a page that refuses us is simply skipped.
"""

from __future__ import annotations

import html as htmllib
import logging
import re
import urllib.parse as up
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, timedelta

log = logging.getLogger("aw_workers.coverage")

MAX_REPORTS = 3  # other reports read per item
MIN_TEXT = 200  # characters of a report's own words for it to count
MIN_SHARED = 0.5  # share of our key words its headline must contain
WINDOW_BEFORE, WINDOW_AFTER = 4, 5  # days around the report's date

_STOP = set("""a an the of to in on for and or with at by from is are was were be been as its it this that these those
over after into about amid near says said say will would can could may might has have had not no new news report reports
video watch photos live update updates latest today day days how why what when where who which after before during while
india indian his her their our your more most than then also just only very per via vs up out off""".split())


@dataclass
class Report:
    publisher: str
    url: str
    title: str
    text: str


def key_words(title: str, limit: int = 8) -> list[str]:
    """The words of a headline that identify the event: names and places first (capitalised), then other content words."""
    toks = re.findall(r"[A-Za-z][A-Za-z'’-]+|\d[\d,.]*", htmllib.unescape(title or ""))
    seen, names, rest = set(), [], []
    for i, t in enumerate(toks):
        low = t.lower().strip("'’")
        if low in _STOP or len(low) < 3 or low in seen:
            continue
        seen.add(low)
        (names if (t[:1].isupper() and i > 0) or t[:1].isdigit() else rest).append(t)
    return (names + rest)[:limit]


def query_for(title: str, when: date | None) -> str:
    words = key_words(title)
    q = " ".join(words)
    if when:
        q += f" after:{(when - timedelta(days=WINDOW_BEFORE)).isoformat()} before:{(when + timedelta(days=WINDOW_AFTER)).isoformat()}"
    return q


def search_url(title: str, when: date | None) -> str:
    return "https://news.google.com/rss/search?" + up.urlencode(
        {"q": query_for(title, when), "hl": "en-IN", "gl": "IN", "ceid": "IN:en"})


def parse_feed(xml: str) -> list[tuple[str, str, str]]:
    """(headline, link, publisher) per result of a Google News RSS search."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        src = it.find("source")
        pub = (src.text or "").strip() if src is not None else ""
        for sep in (" - ", " | "):  # Google appends the publisher to the headline
            if pub and title.endswith(sep + pub):
                title = title[: -len(pub) - len(sep)].strip()
        if title and link:
            out.append((title, link, pub))
    return out


def same_event(ours: str, theirs: str) -> bool:
    mine = {w.lower() for w in key_words(ours, 12)}
    if len(mine) < 2:
        return False
    other = {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z'’-]+|\d[\d,.]*", theirs or "")}
    return len(mine & other) / len(mine) >= MIN_SHARED


def _host(url: str) -> str:
    return (up.urlsplit(url or "").hostname or "").lower().removeprefix("www.")


def _get_feed(url: str) -> str:
    import requests

    from .enrich import TIMEOUT, UA

    r = requests.get(url, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text


def find(title: str, url: str, when: date | None, publisher: str = "", *, get_feed=None, resolve=None, read=None,
         limit: int = MAX_REPORTS) -> list[Report]:
    """Readable reports of the same event by other publishers ([] when none, or when the search fails)."""
    if not key_words(title):
        return []
    get_feed = get_feed or _get_feed
    if resolve is None:
        from .enrich import resolve_google_news as resolve
    if read is None:
        from .merge import page_text as read
    try:
        results = parse_feed(get_feed(search_url(title, when)))
    except Exception as exc:  # noqa: BLE001
        log.info("coverage: search failed for %r: %s", title, type(exc).__name__)
        return []
    own_host, own_pub = _host(url), (publisher or "").strip().lower()
    out, hosts = [], {own_host}
    for head, link, pub in results:
        if len(out) >= limit:
            break
        if not same_event(title, head) or (own_pub and pub.lower() == own_pub):
            continue
        real = resolve(link)
        host = _host(real)
        if host in hosts or real.lower().split("?")[0].endswith(".pdf"):
            continue
        text = read(real) or ""
        if len(text.strip()) < MIN_TEXT:
            continue  # unreadable or refused: skipped, never forced
        hosts.add(host)
        out.append(Report(publisher=pub or host, url=real, title=head, text=text))
    return out


def combined_text(own: str, reports: list[Report], limit: int = 6000) -> str:
    """Our source's words, then each other report's, marked so the summary can be written from all of them."""
    parts = [(own or "").strip()]
    for r in reports:
        parts.append(f"Another report of the same event ({r.publisher}): {r.title}. {r.text.strip()}")
    return "\n\n".join(p for p in parts if p)[:limit]
