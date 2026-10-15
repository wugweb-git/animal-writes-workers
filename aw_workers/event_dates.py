"""Publication dates given as event dates (owner, 15 Oct 2026).

A summary may give a date only when the source says the event happened on it. The model sometimes writes the report's
publication date (the feed date, the dateline) as if it were the event date: "On December 8, 2025, X received an award",
where 8 December was only the day the report came out. The page already shows the publication date.

`pub_date_mentions` finds dates in a summary that equal a publication date and that the source text does not give as
the event date; `strip_mentions` takes such a date phrase out of the sentence without touching anything else.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta

MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4, "may": 5,
    "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9, "sept": 9, "sep": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# Month names are matched capitalised only ("may" and "march" are also verbs).
_MON = r"(?P<mon>(?:January|February|March|April|May|June|July|August|September|October|November|December|" \
       r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec)\.?)"
_WD = r"(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?"
_ORD = r"(?:st|nd|rd|th)?"
_DATE_RES = [
    # 8 December 2025 / 8th of December, 2025 / Monday, 8 December
    re.compile(_WD + r"(?P<day>\d{1,2})" + _ORD + r"\s+(?:of\s+)?" + _MON + r"(?:,?\s+(?P<year>\d{4}))?(?![\w])"),
    # December 8, 2025 / Dec. 8 / Monday, December 8th
    re.compile(_WD + _MON + r"\s+(?P<day>\d{1,2})" + _ORD + r"(?!\d)(?:,?\s+(?P<year>\d{4})(?!\d))?"),
    # 2025-12-08
    re.compile(r"(?P<year>\d{4})-(?P<mnum>\d{2})-(?P<day>\d{2})(?!\d)"),
    # 08/12/2025, 08.12.2025, 08-12-2025 (Indian order: day first)
    re.compile(r"(?<![\d/.-])(?P<day>\d{1,2})[/.-](?P<mnum>\d{1,2})[/.-](?P<year>\d{4})(?!\d)"),
]


@dataclass(frozen=True)
class Mention:
    start: int
    end: int
    text: str
    month: int
    day: int
    year: int | None

    def same_day(self, d: date) -> bool:
        return self.month == d.month and self.day == d.day and (self.year is None or self.year == d.year)


def find_dates(text: str) -> list[Mention]:
    """Day-level dates in `text` (a month or a year alone is not a day and is left alone)."""
    out: list[Mention] = []
    for rx in _DATE_RES:
        for m in rx.finditer(text or ""):
            g = m.groupdict()
            month = int(g["mnum"]) if g.get("mnum") else MONTHS.get((g.get("mon") or "").rstrip(".").lower(), 0)
            day, year = int(g["day"]), int(g["year"]) if g.get("year") else None
            if not (1 <= month <= 12 and 1 <= day <= 31):
                continue
            if any(o.start < m.end() and m.start() < o.end for o in out):
                continue  # already found by an earlier pattern
            out.append(Mention(m.start(), m.end(), m.group(0), month, day, year))
    return sorted(out, key=lambda x: x.start)


def to_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    m = re.match(r"\s*(\d{4})-(\d{2})-(\d{2})", str(value or ""))
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None
    except ValueError:
        return None


# A date in a dateline or byline is the publication date, not the event's.
_BYLINE_BEFORE = re.compile(r"(?i)(?:updated|published|posted|modified|first\s+published|last\s+updated|dated|"
                            r"date|edited|ist|gmt|utc|by\s+[\w .]+)\W{0,6}$|\(@\w+\)\s*$|[;/|]\s*$|[\w/]$")
# The end of a range ("from 2nd - 8th October", "2 October to 8 October") is not the day something happened.
_RANGE_END = re.compile(r"(?i)(?:\d{1,2}(?:st|nd|rd|th)?\s*(?:[-–—]|to|till|until)\s*|\b(?:to|till|until)\s+)$")
_BYLINE_AFTER = re.compile(r"^\s+by\s|^\s*(?:,?\s*(?:at\s+)?\d{1,2}[:.]\d{2}|\s*(?:\(|:|—|–|-\s|\|)|\s+IST\b)")


def _is_dateline(text: str, m: Mention) -> bool:
    before, after = text[max(0, m.start - 40):m.start], text[m.end:m.end + 20]
    if _BYLINE_BEFORE.search(before) or _BYLINE_AFTER.match(after) or _RANGE_END.search(before):
        return True
    if re.match(r"^\s+[A-Z][a-z]", after) and not re.search(r"(?i)\b(?:on|since|from|dated|of)\s+$", before):
        return True  # "Mumbai, June 26, 2026 The Directorate ..." / a tweet's "July 9, 2026 The Standing ...": not in a sentence
    return m.start < 60 and re.match(r"^\s*[,:]", after) is not None  # "NEW DELHI, Dec 8:" at the very top


_PAST = set("""said held met gave given took taken made told came went laid won led sought saw seen began begun began
issued unveiled struck caught found shot hit set put sent spoke wrote ran rose fell dug fed bit bitten killed""".split())


_NOT_NOW = {"are", "is", "am", "even", "till", "until", "still", "of", "for", "from", "as", "to", "that"}


def _past(word: str) -> bool:
    w = word.lower()
    return w in _PAST or (len(w) > 4 and w.endswith("ed"))


def _today_as_event(text: str) -> bool:
    """"The Minister today inaugurated ...", "the case was taken up today": "today" next to a past-tense verb, outside
    quotes (not "a dharma we must uphold today", "Today, caring for animals ...", "farmers are today compelled")."""
    for m in re.finditer(r"\b(?:today|this (?:morning|afternoon|evening))\b(?!'s|’s)", text):
        s = max(text.rfind(". ", 0, m.start()), 0)
        e = text.find(". ", m.end())
        sentence = text[s:e if e > 0 else len(text)]
        if re.search(r"[\"“”]", sentence):
            continue
        prev = re.findall(r"[\w']+", text[max(0, m.start() - 40):m.start()])[-3:]
        nxt = re.findall(r"[\w']+", text[m.end():m.end() + 40])[:2]
        if prev and prev[-1].lower() in _NOT_NOW:
            continue
        if any(_past(w) for w in prev + nxt):
            return True
    return False


def stated_as_event(d: date, source_text: str, pub: date | None = None) -> bool:
    """Does the source itself say something happened on `d`? An explicit date (not a dateline or byline), or a word that
    places it on the report's own day ("today", "on Monday" when the report came out on a Monday) or the day before
    ("yesterday")."""
    text = source_text or ""
    for m in find_dates(text):
        if m.same_day(d) and not _is_dateline(text, m):
            return True
    if pub is not None:
        # "today" as the day of the event ("the court today issued notice"), not "Today, caring for animals ..." or
        # "farmers are today compelled" (now in general); "tonight" is too often in a quote
        if d == pub and _today_as_event(text):
            return True
        if d == pub - timedelta(days=1) and re.search(r"(?i)\b(?:yesterday|last night)\b", text):
            return True
        wd = WEEKDAYS[d.weekday()]
        if 0 <= (pub - d).days <= 6 and re.search(rf"\b(?:[Oo]n|[Ll]ast|[Tt]his|[Ee]arlier on)\s+{wd}\b|\b{wd}\s+(?:morning|afternoon|evening|night)\b", text):
            return True
    return False


def pub_date_mentions(summary: str, pub_dates, source_text: str) -> list[Mention]:
    """Dates in `summary` that equal a publication date and that the source does not give as the event date."""
    pubs = [d for d in (to_date(p) for p in (pub_dates or [])) if d]
    out = []
    for m in find_dates(summary):
        hit = next((p for p in pubs if m.same_day(p)), None)
        if hit and not stated_as_event(hit, source_text, hit):
            out.append(m)
    return out


def without_pub_dates(text: str, pub_dates) -> str:
    """`text` with every date equal to a publication date blanked out: in a rewrite those are not facts to keep."""
    pubs = [d for d in (to_date(p) for p in (pub_dates or [])) if d]
    for m in sorted((m for m in find_dates(text) if any(m.same_day(p) for p in pubs)), key=lambda m: -m.start):
        text = text[:m.start] + " " + text[m.end:]
    return text


# Phrases a date can be taken out of cleanly. Tried in order on the text around one mention.
def _remove(text: str, m: Mention) -> str | None:
    s, e = m.start, m.end
    before, after = text[:s], text[e:]
    # "On December 8, 2025, X did" / "On Monday, 8 December, X" at the start of a sentence
    lead = re.search(r"(?:^|(?<=[.!?]\s))(On|As of|Dated|Since)\s+$", before)
    if lead:
        rest = re.sub(r"^\s*,?\s*", "", after)
        if not rest:
            return None
        return before[:lead.start()] + rest[0].upper() + rest[1:]
    # "An article published on 12 September 2026 discusses" / "The results, released on 3 September 2026, show"
    pub = re.search(r"(,\s*)?\b(?:first\s+)?(?:published|posted|reported|released)\s+(?:on\s+)?$", before, re.I)
    if pub and not re.match(r"^\s*[.!?]", after):
        head = before[:pub.start()]
        if not head.strip() or re.search(r"[.!?]\s*$", head):  # "Released on 28 September 2026, the survey showed"
            rest = re.sub(r"^\s*,?\s*", "", after)
            return head + rest[:1].upper() + rest[1:] if rest else None
        rest = after[1:] if pub.group(1) and after.startswith(",") else after
        return head.rstrip() + rest
    # "X did this on December 8, 2025." / ", on 8 December," mid-sentence
    mid = re.search(r",?\s+(?:on|dated|as of)\s+$", before, re.I)
    if mid:
        rest = after
        if before[mid.start()] == "," and rest.startswith(","):
            rest = rest[1:]
        elif re.match(r",\s+(?:in|at|near|from|by|during|inside|outside|under|on|with)\b", rest):
            rest = rest[1:]  # "occurred on July 28, 2026, in India" → "occurred in India"
        return before[:mid.start()] + rest
    # "(December 8, 2025)"
    if before.endswith("(") and after.startswith(")"):
        return before[:-1].rstrip() + after[1:]
    # "December 8, 2025: X" or "8 December 2025 - X" at the very start
    if not before.strip() and re.match(r"^\s*[:\-–—]\s+", after):
        rest = re.sub(r"^\s*[:\-–—]\s+", "", after)
        return rest[0].upper() + rest[1:] if rest else None
    return None


def _tidy(text: str) -> str:
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r",\s*,", ",", text)
    text = re.sub(r"\(\s*\)", "", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def strip_mentions(summary: str, mentions: list[Mention]) -> str | None:
    """`summary` without those date phrases, every other word unchanged; None when one cannot be taken out cleanly."""
    text = summary
    for m in sorted(mentions, key=lambda x: x.start, reverse=True):
        new = _remove(text, m)
        if new is None:
            return None
        text = new
    return _tidy(text)


# "The concerns were raised." / "The identification of these spots was reported." once the date is out.
_EMPTY_SHORT = re.compile(r"^(?:the|this|these|that|it|such)\b[^,;:]{0,50}?\b(?:was|were|has been|have been|had been)\s+"
                          r"(?:\w+ed|made|held|done|given|taken|seen|shown|brought|put|set|known)(?:\s+(?:up|out|to light))?[.!?]?$",
                          re.I)
_EMPTY = re.compile(
    r"^(?:the|this|that|these|it|they)?\s*(?:\w+[\s-]){0,2}?"
    r"(?:incident|event|decision|ruling|order|judgment|judgement|announcement|action|directive|filing|raid|change|"
    r"development|news|report|article|update|move|meeting|step|development|statement|notice|notification|case|"
    r"information|attack|rescue|seizure|arrest|hearing|observation|verdict|directions?|developments|it|this)?\s*"
    r"(?:(?:has|have)\s+been|was|were|is|are)?\s*"
    r"(?:occurred|took place|happened|made|issued|reported|announced|passed|delivered|published|released|filed|"
    r"posted|shared|dated|given|held|registered|came|surfaced|emerged|recorded)"
    r"(?:\s+(?:to light|out|up))?\s*[.!?]?$", re.I)


def _sentences(text: str) -> list[tuple[int, int]]:
    out, start = [], 0
    for m in re.finditer(r"(?<=[.!?])\s+(?=[A-Z\"“(₹0-9])", text):
        out.append((start, m.start()))
        start = m.end()
    out.append((start, len(text)))
    return out


def remove_dates(summary: str, mentions: list[Mention]) -> str | None:
    """The summary without those dates; a sentence that only gave the date goes as a whole. None = can't be done cleanly."""
    spans = _sentences(summary)
    keep_parts = []
    for s, e in spans:
        sentence = summary[s:e]
        inside = [Mention(m.start - s, m.end - s, m.text, m.month, m.day, m.year) for m in mentions if s <= m.start < e]
        if not inside:
            keep_parts.append(sentence)
            continue
        new = _drop_reported_clause(sentence, inside)
        if new is None:
            new = strip_mentions(sentence, inside)
        if new is None:
            return None
        if _EMPTY.match(new.strip()) or _EMPTY_SHORT.match(new.strip()):
            continue  # the sentence said only when
        keep_parts.append(new)
    out = " ".join(p.strip() for p in keep_parts if p.strip())
    return out or None


def _drop_reported_clause(sentence: str, inside: list[Mention]) -> str | None:
    """"... and was reported on 11 September 2026." / ", reported on X." → the clause goes."""
    if len(inside) != 1:
        return None
    m = inside[0]
    before = sentence[:m.start]
    after = sentence[m.end:]
    if not re.match(r"^\s*[.!?]?\s*$", after):
        return None
    clause = re.search(r"(?:,?\s+and|,)\s+(?:was|were|has been|had been)?\s*(?:first\s+)?(?:reported|published|announced|"
                       r"released|posted|shared|made public)\s+(?:on|in)\s+$", before, re.I)
    if not clause:
        return None
    return before[:clause.start()].rstrip(" ,") + (after.strip() or ".")


def clean_removal(before: str, after: str) -> bool:
    """Only words taken out, none added, and every sentence still starts and ends properly."""
    words = Counter(re.findall(r"[\w'’-]+", before.lower()))
    words.subtract(Counter(re.findall(r"[\w'’-]+", after.lower())))
    if any(n < 0 for n in words.values()):
        return False
    for s, e in _sentences(after):
        sentence = after[s:e].strip()
        if not sentence or not re.match(r"[A-Z0-9\"“‘'(₹]", sentence) or re.search(r"\s[,.;:]|,,|\.\.|,\s*\.$", sentence):
            return False
    return True


def problem(mentions: list[Mention]) -> str:
    return ("gives the publication date as the event date (" + "; ".join(m.text for m in mentions)
            + "): the source does not say the event happened then, so leave that date out")
