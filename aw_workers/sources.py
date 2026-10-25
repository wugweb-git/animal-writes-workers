"""Fetch and parse sources into plain Item records."""

from __future__ import annotations

import html
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup

from . import keywords as _keywords
from . import matcher
from . import relay as _relay

log = logging.getLogger(__name__)

USER_AGENT = "AnimalWritesWorkers/1.0 (+https://animalwrites.in)"
# News search engines answer bots with 503 more often; they get a browser user agent, retries and a gap between calls.
BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
SEARCH_HOSTS = ("news.google.com", "www.bing.com", "bing.com")
SEARCH_GAP = 2.0  # seconds between two calls to a search engine
RETRY_WAITS = (5, 20)  # waits before the 2nd and 3rd try on 429/5xx
TIMEOUT = 30
_last_search = {"t": 0.0}


def ca_bundle() -> str:
    """The usual CA bundle plus the intermediates in aw_workers/certs that some government sites do not send (eGazette:
    Let's Encrypt YR1/YR2 and Root YR; AWBI: eMudhra's DV TLS chain, refused on 18-19 Oct 2026). The file name holds a
    hash of the contents, so a changed bundle is never read from an old copy."""
    import hashlib
    import tempfile

    import certifi

    text = Path(certifi.where()).read_text()
    for pem in sorted((Path(__file__).parent / "certs").glob("*.pem")):
        text += "\n" + pem.read_text()
    path = Path(tempfile.gettempdir()) / f"aw-ca-bundle-{hashlib.sha1(text.encode()).hexdigest()[:12]}.pem"
    if not path.exists():
        path.write_text(text)
    return str(path)


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
    max_items: int | None = None  # overrides max_items_per_source (keyword searches: 40)
    skip_urls: list[str] = field(default_factory=list)  # rows already on the page when we started watching it
    file_as: str = ""  # "press": items go to Media & PR (coverage of a campaign topic) instead of the section
    categories: list[str] = field(default_factory=list)  # a keyword search: the categories of the keyword that found it


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
    employment_type: str = ""  # opportunities: hours as published ("Type: Full time"), in Google's JobPosting values
    doc_kind: str = ""  # Library documents: judgment | order | notification | rule | act
    court: str = ""  # judgments: supreme-court | high-court
    document_url: str = ""  # the PDF, when there is one
    legal_number: str = ""  # Gazette notifications: G.S.R. / S.O. number, e.g. "G.S.R. 864(E)"
    draft: bool = False  # Gazette notifications: a draft published for objections, not rules in force


CATEGORIES: dict[str, list[str]] = {}
# Each section's allowed categories and tags (the site's Settings → Sourcing → Sections): site type => {"categories",
# "tags"} (slug lists or slug => name). Empty until the site sends them; then items are classified only within these.
SECTIONS: dict[str, dict] = {}
# Topic tags (e.g. a campaign): tag slug => {"any": [phrases], "hold": [regexes]}. An item whose title or text has one of
# the phrases gets the tag (if the section allows it); one that also matches a "hold" pattern (and none of the "unless"
# words) waits for a person.
TOPIC_TAGS: dict[str, dict] = {}
SITE_TYPES = {"news": "aw_news", "legal": "aw_legal", "opportunity": "aw_opportunity", "research": "post"}
# The lists came with the keyword table (phase 2): categories at ingest start from the keyword that found the item.
KEYWORD_TABLE = {"on": False}


def keyword_table_on() -> bool:
    return KEYWORD_TABLE["on"]


def set_sections(data) -> None:
    if isinstance(data, dict) and data:
        SECTIONS.clear()
        SECTIONS.update({str(k): v for k, v in data.items() if isinstance(v, dict)})


def _allowed(item_type: str, key: str) -> list[str] | None:
    sec = SECTIONS.get(SITE_TYPES.get(item_type, item_type))
    if not sec or key not in sec:
        return None
    vals = sec[key]
    return list(vals.keys()) if isinstance(vals, dict) else [str(v) for v in (vals or [])]


def allowed_categories(item_type: str) -> list[str]:
    """Category slugs this kind of item may get (every known category until the site sends its lists)."""
    allowed = _allowed(item_type, "categories")
    return allowed if allowed else list(CATEGORIES)


def allowed_tags(item_type: str) -> list[str] | None:
    """Tag slugs this kind of item may get, or None when the site has not sent its lists."""
    return _allowed(item_type, "tags")


def categories_for(item: "Item", extra: str = "") -> list[str]:
    """Category slugs whose keywords occur in the item's title, feed text or `extra` (e.g. our summary)."""
    hay = f" {item.title} {item.text} {extra} ".lower()
    found = []
    word = matcher.word_mode()
    for slug, words in CATEGORIES.items():
        for w in words:
            w = w.lower()
            if word:
                if matcher.matches(hay, w):
                    found.append(slug)
                    break
                continue
            if (re.search(rf"\b{re.escape(w)}\b", hay) if w.isascii() and len(w) <= 4 else w in hay):
                found.append(slug)
                break
    return found


def load_sources(path: str | Path = "sources.yaml") -> tuple[list[Source], dict]:
    return load_sources_data(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


# The relevance lists a keyword check by role reads (sourcing policy `relevance`): "include" is the strong list. A check
# keeps items that have one of its words, so the Exclude role can never be one (it would keep what it should drop).
ROLE_LISTS = {"include": "strong", "strong": "strong", "animal": "animal", "context": "context"}
_warned: set[str] = set()  # each malformed-block warning once per run


V2_BLOCKS = ("schema", "match", "sources_v2", "category_names", "keywords_v2")  # sourcing restructure: beside the v1 keys


def with_v2_blocks(config: dict, raw: dict) -> dict:
    """The site's lists with the new blocks the policy sends beside them (top level or inside `config`); `config`
    wins when both have one. Old workers ignore these keys; sources.yaml gets them through Sources sync. The keyword
    table (phase 2) comes as top-level `keywords` (objects) and as `config.keywords_v2`; inside the lists it is always
    `keywords_v2`, because `keywords` there is the v1 list of Include words."""
    out = dict(config)
    for k in V2_BLOCKS:
        if k not in out and k in (raw or {}):
            out[k] = raw[k]
    if "keywords_v2" not in out and _keywords.raw_block({"keywords": (raw or {}).get("keywords")}) is not None:
        out["keywords_v2"] = raw["keywords"]
    return out


def v2_sources(data: dict) -> list[dict] | None:
    """The site's `sources_v2` list when it is there and every entry has the keys a source needs; None otherwise
    (sources.yaml from before phase 1, or a malformed block), so the v1 `sources` list is read."""
    v2 = (data or {}).get("sources_v2")
    if not isinstance(v2, list) or not v2:
        return None
    for s in v2:
        if not isinstance(s, dict) or not all(s.get(k) for k in ("id", "name", "kind", "type", "url")):
            what = repr(s if isinstance(s, dict) else type(s))[:200]
            if what not in _warned:
                _warned.add(what)
                log.warning("sources_v2 has an incomplete entry (%s): reading the v1 sources list", what)
            return None
    return v2


def check_words(s: dict, data: dict, relevance: dict | None = None, kws: list[dict] | None = None) -> list[str] | None:
    """The words a `sources_v2` keyword check names (`check: {roles: [...], categories: [...]}`), or None when the
    check names nothing (or nothing it names has words): the source's own `filter` / `keywords` then apply, as in v1.
    The source's "Keyword check" switch still decides: `filter: false` means no check, whatever `check` names.
    With the keyword table (`kws`, phase 2) a role reads the spellings of the keywords with that role and a category
    the spellings of the `category` keywords mapped to it. Without it, the relevance lists; without those
    (sources.yaml fallback) the Include role reads the lists' `keywords`, which the site fills with the Include list."""
    if s.get("filter") is False:
        return None
    check = s.get("check") if isinstance(s.get("check"), dict) else {}
    roles = [str(r).lower() for r in check.get("roles") or []]
    cats = [str(c) for c in check.get("categories") or []]
    if not roles and not cats:
        return None
    rel = _keywords.relevance_lists(kws) if kws is not None else (relevance or {})
    words: list[str] = []
    for r in roles:
        key = ROLE_LISTS.get(r)
        if key is None:
            log.warning("source %s: keyword check role %r is not one a check can use; ignored", s.get("id"), r)
            continue
        found = rel.get(key)
        if not found and key == "strong" and kws is None:
            found = data.get("filter_words") or data.get("keywords")
        words += [str(w) for w in found or []]
    cat_words = _keywords.category_words(kws) if kws is not None else (data.get("categories") or {})
    for c in cats:
        words += [str(w) for w in cat_words.get(c) or []]
    words = list(dict.fromkeys(w for w in words if w.strip()))
    if not words:
        log.warning("source %s: its keyword check (%s) has no words; using its own keyword list", s.get("id"), check)
        return None
    return words


def relay_hosts(data: dict, v1_hosts) -> list[str]:
    """Hosts the site fetches for us: the policy's `relay_hosts` plus the publisher host of every `sources_v2` source
    whose relay switch is on (the site builds `relay_hosts` from the same switches, so this only ever adds a host the
    site already allows)."""
    hosts = [str(h).lower().strip(".") for h in (v1_hosts or []) if str(h).strip()]
    for s in v2_sources(data) or []:
        h = str(s.get("host") or "").lower().strip(".")
        if s.get("relay") and h:
            hosts.append(h)
    return list(dict.fromkeys(hosts))


def load_sources_data(data: dict, relevance: dict | None = None) -> tuple[list[Source], dict]:
    """Sources from the lists' dict: sources.yaml, or the site's copy (Settings → Sourcing), which has the same shape.
    The site's `sources_v2` block is read when it is there (each entry is a v1 source plus `host`, `relay`, `read_by`
    and `check`), the v1 `sources` list otherwise. `match: "word"` switches every keyword check to whole words. With
    the keyword table (`keywords_v2`, phase 2) the category words come from the `category` keywords and keyword checks
    from the roles; without it, from the v1 lists."""
    kws = _keywords.from_data(data)
    KEYWORD_TABLE["on"] = kws is not None
    CATEGORIES.clear()
    if kws is not None:
        CATEGORIES.update(_keywords.category_words(kws, slugs=list(data.get("category_names") or {})))
    else:
        CATEGORIES.update({k: [str(w) for w in v] for k, v in (data.get("categories") or {}).items()})
    set_sections(data.get("sections"))
    matcher.set_mode(data.get("match"))
    TOPIC_TAGS.clear()
    TOPIC_TAGS.update({str(k): v for k, v in (data.get("topic_tags") or {}).items() if isinstance(v, dict)})
    sources = []
    filter_words = list(data.get("filter_words") or data.get("keywords") or [])
    v2 = v2_sources(data)
    for s in v2 if v2 is not None else data.get("sources", []):
        # Settings → Sourcing switches (the site applies them too): a source switched off is not fetched; "Keyword
        # check" gives it the keyword list. Lists without the switches (sources.yaml) are read as they are.
        if s.get("on") is False:
            continue
        words = check_words(s, data, relevance, kws) if v2 is not None else None
        if words is not None:
            s = {**s, "keywords": words}
        elif s.get("filter"):
            s = {**s, "keywords": filter_words}
        sources.append(source_from_dict(s))
    return sources, data.get("defaults", {})


def source_from_dict(s: dict, categories=()) -> Source:
    """One source from its dict (a v1 source, a `sources_v2` entry or a keyword search that carries a whole feed)."""
    if s["type"] not in ("news", "legal", "opportunity"):
        raise ValueError(f"source {s['id']}: type must be news, legal or opportunity")
    if s["kind"] not in PARSERS and s["kind"] not in SELF_FETCH:
        raise ValueError(f"source {s['id']}: unknown kind {s['kind']}")
    return Source(
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
        max_items=s.get("max_items"),
        skip_urls=list(s.get("skip_urls") or []),
        file_as=str(s.get("file_as") or ""),
        categories=list(categories or []),
    )


# A list page, not one story: a tag, category, topic, author or search page (a "peta-india" title came from
# telanganatoday.com/tag/peta-india on 15 Oct 2026). Its "title" is then only the address name.
LISTING_PATH = re.compile(r"/(?:tags?|topics?|category|categories|author|authors|search|section)/", re.I)
SLUG_TITLE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)+$")


# Not news (owner, 20 Oct 2026): a video page or a "watch"/viral clip post, and a list of institutions or courses
# ("Top Veterinary Science Universities in India: Check List of Institutes", "Video of Can We Protect ...",
# "Watch these small scared rabbits ...", "India's fat dog vote goes viral").
VIDEO_PATH = re.compile(r"/(?:videos?|watch|vi-[A-Za-z0-9]+|shorts|reels?)(?:/|$)|youtube\.com|youtu\.be", re.I)
VIDEO_TITLE = re.compile(r"^\s*(?:watch|video|viral video)\b|^\s*video of\b|\((?:watch|video)\)|\bgoes viral\b|\bviral (?:video|clip|post)\b", re.I)
# Only a ranking or list of universities, colleges, institutes or courses ("Top veterinary colleges in India"); never
# news that names schools or hospitals ("India top court: No dogs in schools, hospitals ...", owner 20 Oct 2026).
_INST = r"(?:universit(?:y|ies)|colleges|institutes|institutions|courses)"
LISTICLE_TITLE = re.compile(
    rf"\b(?:top|best)\s+(?:\d+\s+)?(?:[\w&-]+\s+){{0,4}}{_INST}\b|\b(?:list|ranking|rankings)\s+of\s+(?:the\s+)?(?:[\w&-]+\s+){{0,4}}{_INST}\b"
    rf"|\b{_INST}\b.{{0,40}}\b(?:check list|ranking|rankings)\b",
    re.I,
)


# An archive or tag page by its headline (owner, 20 Oct 2026: Live Law's "Read all latest updates on and about Tamil
# Nadu Forest And Wildlife Crime Control Bureau"): "Latest news on ...", "Latest News, Photos, Videos on ...".
ARCHIVE_TITLE = re.compile(
    r"^\s*(?:read\s+)?(?:all\s+)?(?:the\s+)?latest\s+(?:news|updates|stories|headlines|articles)\b(?:\s*(?:,|and|&)\s*\w+){0,3}"
    r"\s+(?:on|about|from|for|of)\b|\blatest\s+news\s*,\s*(?:photos|videos|updates)\b|^\s*(?:all\s+)?(?:news|updates)\s+(?:on\s+and\s+)?about\b",
    re.I,
)

# A job vacancy, which belongs in Opportunities (from the opportunity sources), never in News (owner, 20 Oct 2026:
# "Legal officer (young professional II) vacancy at ...", "AWBI legal advisor recruitment 2026 - apply offline").
# A news story about vacancies ("flags 63.81% vacancies in Bandipur Tiger Reserve", "recruitment scam") is not caught.
JOB_PATH = re.compile(r"/(?:jobs?|job-updates?|careers?|recruitments?|vacanc(?:y|ies))(?:/|-)", re.I)
JOB_HOSTS = re.compile(r"(?:^|\.)(?:freejobalert|sarkariresult|sarkarinaukri\w*|employmentnews|naukri|indeed|"
                       r"freshersworld|jobsarkari|govtjobsalert\w*)\.(?:com|in|co\.in|gov\.in|org)$", re.I)
JOB_TITLE = re.compile(
    r"\bvacancy\s+(?:at|in|for|with)\b|\bvacancies\s+(?:at|in|for|with)\b.{0,80}\b(?:apply|recruitment|notification|last\s+date)\b"
    r"|\brecruitment\s+20\d\d\b|\b(?:apply|applications?)\s+(?:online|offline|now|invited)\b|\bwalk[- ]in\s+interview\b"
    r"|\b(?:job|career)\s+(?:opening|openings|alert|notification|vacancy)\b|\bnow\s+hiring\b|\bis\s+hiring\b",
    re.I,
)


def job_vacancy(url: str, title: str) -> bool:
    """True when the link is a job advertisement rather than a news report."""
    parts = urlsplit(url or "")
    return bool(JOB_HOSTS.search((parts.hostname or "").lower()) or JOB_PATH.search(parts.path + "/")
                or JOB_TITLE.search(title or ""))


def not_an_article(url: str, title: str, kind: str = "news") -> str:
    """Why this link is not a single news story ("" when it is). `kind` is the source's type: a job advertisement is
    only refused from sources that are not opportunity sources."""
    path = urlsplit(url or "").path
    if LISTING_PATH.search(path + "/"):
        return "list page (tag, category, topic, author or search)"
    if ARCHIVE_TITLE.search(title or ""):
        return "an archive or tag page of a news site, not a news report"
    if kind != "opportunity" and job_vacancy(url, title):
        return "a job vacancy, not a news report (jobs come from the opportunity sources)"
    if SLUG_TITLE.match((title or "").strip().strip("\"'‘’“”").lower()) and " " not in (title or "").strip():
        return "title is only an address name"
    if VIDEO_PATH.search(url or "") or VIDEO_TITLE.search(title or ""):
        return "a video or viral clip post, not a news report"
    if LISTICLE_TITLE.search(title or ""):
        return "a list of institutions or courses, not a news report"
    return ""


# A headline copied from a press release (owner, 20 Oct 2026: "FIR Registered for Sexual Abuse of a Female Dog in
# Bhojpur to Protect ALL Females, Following PETA India Intervention"): wire services, or an NGO's own release whose
# headline shouts (a word in capitals), credits the NGO ("Following PETA India Intervention") or runs long. The item
# is sent for review with this reason so a person writes a plain headline.
PR_HOSTS = re.compile(r"(?:^|\.)(?:prnewswire|businesswire|globenewswire|newswire|einpresswire|pressreleasepoint|"
                      r"openpr|issuewire)\.(?:com|in|org|net)$", re.I)
NGO_RELEASE = re.compile(r"(?:^|\.)(?:petaindia\.com|hsi\.org|hsiindia\.org|wildlifesos\.org|fiapo\.org|wwfindia\.org)$", re.I)
PR_WORDING = re.compile(r"\bfollowing [\w.]+(?: [\w.]+){0,3} (?:intervention|complaint|appeal|efforts?)\b|\bannounces?\b|"
                        r"\bto protect all\b", re.I)
SHOUTED = re.compile(r"\b(?:ALL|NOW|NEW|FREE|MUST|URGENT|BREAKING|NOT|NEVER|EVERY|STOP|SAVE|JUSTICE)\b")  # words, not acronyms


def press_release_headline(url: str, title: str) -> str:
    """Why the headline reads as copied from a press release ("" when it does not)."""
    host = (urlsplit(url or "").hostname or "").lower()
    t = (title or "").strip()
    if PR_HOSTS.search(host):
        return "headline copied from a press release (wire service): write a plain headline"
    if NGO_RELEASE.search(host) and (PR_WORDING.search(t) or SHOUTED.search(t) or len(t) > 90):
        return "headline copied from an organisation's press release: write a plain headline"
    return ""


def is_search(url: str) -> bool:
    return (urlsplit(url).hostname or "").lower() in SEARCH_HOSTS


def fetch(url: str, get=requests.get, sleep=time.sleep) -> str:
    """GET a feed. Search engines (Google News, Bing News): browser user agent, a gap between calls, and up to two
    retries on 429/5xx (Google answers GitHub's servers with 503 at times, 12 Oct 2026). Other sites refusing our
    agent with 403 are asked once more with a browser's (Substack refuses GitHub's servers, 16-20 Oct 2026)."""
    search = is_search(url)
    headers = {"User-Agent": BROWSER_UA if search else USER_AGENT}
    waits = RETRY_WAITS if search else ()
    for attempt in range(len(waits) + 1):
        if search:
            gap = SEARCH_GAP - (time.monotonic() - _last_search["t"])
            if gap > 0:
                sleep(gap)
            _last_search["t"] = time.monotonic()
        try:
            resp = get(url, headers=headers, timeout=TIMEOUT)
        except (requests.ConnectionError, requests.Timeout):
            # Unreachable from GitHub (Indian government sites time out there at times, 20 Oct 2026): through the site.
            text = _relay.fetch(url) if _relay.allowed(url) else None
            if text is None:
                raise
            return text
        if resp.status_code == 403 and headers["User-Agent"] != BROWSER_UA:
            headers = {"User-Agent": BROWSER_UA}
            resp = get(url, headers=headers, timeout=TIMEOUT)
        if resp.status_code == 403 and _relay.allowed(url):  # refused from GitHub: the site fetches it for us
            text = _relay.fetch(url)
            if text is not None:
                return text
        if attempt < len(waits) and (resp.status_code == 429 or resp.status_code >= 500):
            sleep(waits[attempt])
            continue
        resp.raise_for_status()
        resp.encoding = resp.encoding or "utf-8"
        return resp.text
    raise RuntimeError("unreachable")


def bing_fallback(source: "Source") -> "Source | None":
    """The same search on Bing News RSS (India edition, past week), for when Google News fails. None if the source is
    not a Google News search. Bing's own links are unwrapped to the publisher URL by parse_rss."""
    from dataclasses import replace

    parts = urlsplit(source.url)
    if parts.hostname != "news.google.com" or not parts.path.startswith("/rss/search"):
        return None
    q = parse_qs(parts.query).get("q", [""])[0]
    q = re.sub(r"\s*\bwhen:\S+", "", q).strip()
    if not q:
        return None
    url = "https://www.bing.com/news/search?" + urlencode({"q": q, "format": "rss", "mkt": "en-IN", "qft": 'interval="8"'})
    return replace(source, url=url, lookback_days=min(source.lookback_days or 3, 3))


def site_search_fallback(source: "Source") -> "Source | None":
    """A publisher's own feed that refuses GitHub's servers (PFA's Substack answers 403 there, 200 elsewhere): the
    publisher's stories from the last week through Google News (site: search). None for search engines and pages
    that are not feeds. The source's keywords still apply."""
    from dataclasses import replace

    host = (urlsplit(source.url).hostname or "").lower()
    if source.kind != "rss" or not host or is_search(source.url):
        return None
    url = "https://news.google.com/rss/search?" + urlencode({"q": f"site:{host} when:7d", "hl": "en-IN", "gl": "IN", "ceid": "IN:en"})
    return replace(source, url=url, lookback_days=min(source.lookback_days or 7, 7))


def _unwrap_bing(link: str) -> str:
    """bing.com/news/apiclick.aspx?...&url=<publisher URL> -> the publisher URL."""
    parts = urlsplit(link)
    if (parts.hostname or "").endswith("bing.com") and "apiclick" in parts.path:
        real = parse_qs(parts.query).get("url", [""])[0]
        if real.startswith("http"):
            return real
    return link


def _clean(text: str) -> str:
    text = BeautifulSoup(html.unescape(text or ""), "html.parser").get_text(" ")
    return re.sub(r"\s+", " ", text).strip()


def parse_rss(source: Source, body: str) -> list[Item]:
    feed = feedparser.parse(body.lstrip("﻿"))
    items = []
    for e in feed.entries:
        title = _clean(e.get("title", ""))
        link = _unwrap_bing((e.get("link") or "").strip())
        if not title or not link:
            continue
        when = None
        if e.get("published"):
            try:
                when = parsedate_to_datetime(e.published).astimezone(timezone.utc).date()
            except (TypeError, ValueError):
                when = None
        publisher = _clean((e.get("source") or {}).get("title", "") or e.get("news_source", ""))
        if publisher and title.endswith(f" - {publisher}"):
            title = title[: -len(publisher) - 3].strip()
        item = Item(source, title, link, when, _clean(e.get("summary", "")), publisher)
        item.image = feed_image(e)
        items.append(item)
    return items


def feed_image(e) -> str:
    """The story's picture as the feed gives it (owner, 18 Oct 2026): media:content, media:thumbnail, an image
    enclosure, else the first picture in the post. Feeds reach sites that block page reads (PETA India, NDTV)."""
    for m in list(e.get("media_content") or []) + list(e.get("media_thumbnail") or []):
        url = (m.get("url") or "").strip()
        if url.startswith("https://") and (m.get("medium") in (None, "", "image") or "image" in (m.get("type") or "image")):
            return url
    for enc in e.get("enclosures") or []:
        url = (enc.get("href") or enc.get("url") or "").strip()
        if url.startswith("https://") and (enc.get("type") or "").startswith("image/"):
            return url
    for c in [*(x.get("value", "") for x in (e.get("content") or [])), e.get("summary", "")]:
        m = re.search(r'<img[^>]+src=["\']([^"\']+)', c or "", re.I)
        if m and m.group(1).startswith("https://"):
            return html.unescape(m.group(1))
    return ""


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


_EMPLOYMENT = {"fulltime": "FULL_TIME", "parttime": "PART_TIME", "contract": "CONTRACTOR", "contractual": "CONTRACTOR",
               "temporary": "TEMPORARY", "internship": "INTERN", "intern": "INTERN", "volunteer": "VOLUNTEER"}


def employment_type(text: str) -> str:
    """The employment type a posting states ("Type: Fulltime", "Full time"), as Google's value; "" when not stated."""
    value = re.sub(r"[^a-z]", "", (text or "").lower())
    return _EMPLOYMENT.get(value, "")


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
                                  place=place, deadline=deadline_in(desc), employment_type=employment_type(j.get("Job_Type") or "")))
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
        stated = re.search(r"\bType:\s*([A-Za-z -]{3,20}?)(?:\s+Experience:|$)", _clean(it.get_text(" ")))
        items.append(Item(source, title, f"{source.url.split('#')[0]}#{anchor}", None, text[:1500],
                          place=_clean(place_el.get_text(" ")) if place_el else "", deadline=deadline_in(text),
                          employment_type=employment_type(stated.group(1)) if stated else ""))
    return items


def parse_ccsea(source: Source, body: str) -> list[Item]:
    """CCSEA "What's New": a table of rows (number, title, PDF link) with no dates; rows in skip_urls are left out."""
    soup = BeautifulSoup(body, "html.parser")
    skip = {u.strip() for u in source.skip_urls}
    items = []
    for row in soup.select("tr"):
        cells = row.find_all("td")
        link = row.select_one("a[href]")
        if len(cells) < 2 or link is None:
            continue
        title = _clean(cells[1].get_text(" "))
        url = urljoin(source.url, link["href"].strip()).replace(" ", "%20")
        if title and url not in skip:
            kind = "rule" if re.search(r"\brules?\b", title, re.I) else "circular"
            items.append(Item(source, title, url, None, doc_kind=kind, document_url=url))
    return items


def topic_tags_for(text: str) -> tuple[list[str], str]:
    """Topic tags whose phrases occur in the text, and the first "hold" reason that matches (or "")."""
    hay = f" {text} ".lower()
    word = matcher.word_mode()

    def has(w) -> bool:
        return matcher.matches(hay, str(w)) if word else str(w).lower() in hay

    tags, hold = [], ""
    for slug, rule in TOPIC_TAGS.items():
        if any(has(w) for w in rule.get("any") or []):
            tags.append(slug)
            unless = [str(w) for w in rule.get("unless") or []]
            for pat in rule.get("hold") or []:
                if not hold and re.search(str(pat), text, re.I) and not any(has(w) for w in unless):
                    hold = f"{slug}: {rule.get('hold_reason') or pat}"
    return tags, hold


PARSERS = {"rss": parse_rss, "awbi": parse_awbi, "zoho": parse_zoho, "sjm": parse_sjm, "ccsea": parse_ccsea}


def matches_keywords(item: Item, keywords: list[str]) -> bool:
    if not keywords:
        return True
    hay = (item.title if item.source.match == "title" else f"{item.title} {item.text}").lower()
    if matcher.word_mode():
        return bool(matcher.any_match(hay, keywords))
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
    # Newest first: search feeds are ordered by relevance, and a cap on that order kept the same old stories every run
    # (no new News 10-12 Oct 2026). Stable sort, so items of the same day keep the feed's order.
    fresh.sort(key=lambda i: i.date, reverse=True)
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
