"""The shapes the enrichment worker passes around: a Fact (one value from one source), and small text helpers."""

from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

import requests

IST = timezone(timedelta(hours=5, minutes=30))
UA = "Mozilla/5.0 (compatible; AnimalWritesWorkers/1.0; +https://animalwrites.in)"
BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
TIMEOUT = 20

STORE, CHECK, LEAD = "store", "check", "lead"


def now_ist() -> str:
    return datetime.now(IST).replace(microsecond=0).isoformat()


@dataclass
class Fact:
    """One value for one field from one source. role: store (may be kept), check (Google: compare only), lead (a link to
    follow, e.g. the website Google lists)."""

    field: str
    value: object
    source_key: str
    source_name: str
    source_url: str = ""
    role: str = STORE
    fetched_at: str = field(default_factory=now_ist)
    note: str = ""

    def ref(self) -> dict:
        return {"key": self.source_key, "name": self.source_name, "url": self.source_url, "fetched_at": self.fetched_at}


@dataclass
class Context:
    """What one listing's sources share: the listing from the site, leads found so far, and page text for the AI pass."""

    listing: dict
    facts: list = field(default_factory=list)
    texts: list = field(default_factory=list)  # (source_key, url, text)
    leads: dict = field(default_factory=dict)  # website / instagram / facebook ... -> url
    status: dict = field(default_factory=dict)  # source key -> "ok" | "no match" | "skipped: why" | "error: why"
    google_cost: float = 0.0

    def add(self, *facts: Fact) -> None:
        self.facts += [f for f in facts if f is not None and f.value not in ("", None, [], {})]

    @property
    def name(self) -> str:
        return self.listing.get("name") or ""


class Polite:
    """One requests session with a minimum gap between calls to the same host."""

    def __init__(self, gap: float = 1.0, session=None, sleep=time.sleep):
        self.session = session or requests.Session()
        self.gap, self.sleep, self.last = gap, sleep, {}

    def get(self, url: str, gap: float | None = None, **kw):
        return self._go("GET", url, gap, **kw)

    def post(self, url: str, gap: float | None = None, **kw):
        return self._go("POST", url, gap, **kw)

    def _go(self, method, url, gap, **kw):
        host = re.sub(r"^https?://([^/]+).*$", r"\1", url)
        wait = (gap if gap is not None else self.gap) - (time.monotonic() - self.last.get(host, 0))
        if wait > 0:
            self.sleep(wait)
        kw.setdefault("timeout", TIMEOUT)
        kw.setdefault("headers", {}).setdefault("User-Agent", UA)
        try:
            return self.session.request(method, url, **kw)
        finally:
            self.last[host] = time.monotonic()


_STOP = {"the", "of", "and", "for", "a", "an", "&", "in", "at", "society", "trust", "foundation", "charitable", "sangh",
         "samiti", "sanstha", "organisation", "organization", "association", "india", "indian", "regd", "registered",
         "pvt", "ltd", "private", "limited", "ngo"}


def fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9 ]+", " ", s)


def core_tokens(name: str) -> list[str]:
    return [t for t in fold(name).split() if t not in _STOP and len(t) > 1]


def name_similarity(a: str, b: str) -> float:
    """0..1: how alike two organisation names are (common words like Trust or Society do not count)."""
    ta, tb = core_tokens(a), core_tokens(b)
    if not ta or not tb:
        return 0.0
    sa, sb = set(ta), set(tb)
    common = len(sa & sb)
    overlap = common / min(len(sa), len(sb))
    ratio = SequenceMatcher(None, " ".join(ta), " ".join(tb)).ratio()
    weight = 0.95 if common >= 2 or min(len(sa), len(sb)) == 1 else 0.75  # one shared word of several is weak
    return round(max(ratio, overlap * weight), 3)


def _share(toks: list[str], hay_words: set[str]) -> float:
    """Share of the name's significant words in the text; a word of 7+ letters also counts when the text has one very like
    it (Panjrapole / Panjarpol)."""
    if not toks:
        return 0.0
    hit = 0
    for t in toks:
        if t in hay_words or (len(t) >= 7 and any(abs(len(w) - len(t)) <= 2 and SequenceMatcher(None, t, w).ratio() >= 0.8 for w in hay_words)):
            hit += 1
    return hit / len(toks)


def names_in_text(name: str, text: str) -> float:
    """Best share of the name's significant words found in a page's text, for the whole name, the part outside brackets and
    the part inside them ("The Paws Pack (Resq Charitable Trust)": either name counts)."""
    hay = set(fold(text).split())
    inside = " ".join(re.findall(r"\(([^)]*)\)", name))
    outside = re.sub(r"\([^)]*\)", " ", name)
    return max(_share(core_tokens(v), hay) for v in (name, outside, inside) if core_tokens(v)) if core_tokens(name) else 0.0


PIN = re.compile(r"\b([1-9]\d{2})\s?(\d{3})\b")


def pincode_in(text: str) -> str:
    m = PIN.search(text or "")
    return m.group(1) + m.group(2) if m else ""


def norm_phone(s: str) -> str:
    """Indian numbers as written for people: mobile 98765 43210, landline with STD code 0xx-xxxxxxx; '' when not a number."""
    d = re.sub(r"\D", "", s or "")
    if d.startswith("91") and len(d) == 12:
        d = d[2:]
    if re.match(r"^\s*\(?0\d{2,4}\)?[\s-]", s or "") and d.startswith("0") and 10 <= len(d) <= 12:
        return d  # written with its STD code apart: a landline, e.g. (080) 2860 1985
    if d.startswith("0") and len(d) == 11 and d[1] in "6789":
        d = d[1:]
    if len(d) == 10 and d[0] in "6789":
        return f"{d[:5]} {d[5:]}"
    if d.startswith("0") and 10 <= len(d) <= 12:
        return d
    if len(d) == 10 and d[0] in "12345":  # landline without the leading 0
        return "0" + d
    if d.startswith("1800") and len(d) in (10, 11):
        return d
    return ""


def norm_url(u: str) -> str:
    u = (u or "").strip()
    if not u:
        return ""
    if not re.match(r"^https?://", u, re.I):
        u = "https://" + u.lstrip("/")
    return re.sub(r"[?#].*$", "", u).rstrip("/")


def same_value(field: str, a, b) -> bool:
    if field in ("phone", "whatsapp", "emergency"):
        return bool(norm_phone(str(a))) and norm_phone(str(a))[-10:].replace(" ", "") == norm_phone(str(b))[-10:].replace(" ", "")
    if field in ("lat", "lng"):
        try:
            return abs(float(a) - float(b)) < 0.002  # about 200 m
        except (TypeError, ValueError):
            return False
    if isinstance(a, (list, dict)) or isinstance(b, (list, dict)):
        return a == b
    sa, sb = str(a).strip().lower(), str(b).strip().lower()
    if re.match(r"^https?://", sa) or re.match(r"^https?://", sb):
        strip = lambda u: re.sub(r"^https?://(www\.|m\.)?", "", norm_url(u).lower())  # noqa: E731
        return strip(sa) == strip(sb)
    if field == "address":
        return pincode_in(sa) and pincode_in(sa) == pincode_in(sb) and name_similarity(sa, sb) >= 0.6
    return re.sub(r"[\s,.]+", " ", sa) == re.sub(r"[\s,.]+", " ", sb)
