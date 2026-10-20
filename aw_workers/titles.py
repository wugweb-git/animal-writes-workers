"""Titles without hyphens (owner, 18 Oct 2026, standing: no extra dashes or hyphens).

The same rule as the site's aw_seo_no_hyphens(): a hyphen or dash joining two words becomes a space
("Human-Elephant" -> "Human Elephant", "WII-SACON" -> "WII SACON"), a year range reads "2025 to 2026" (also "2025-26"),
another number range "5 to 10". In a title already in Title Case the word after the hyphen starts with a capital unless
it is a connector or a unit ("Bull-and-Horse" -> "Bull and Horse", "8.5-kg" -> "8.5 kg").
"""

from __future__ import annotations

import re

_D = "[-‐-―−]"
_SMALL = {"a", "an", "the", "and", "or", "nor", "but", "of", "in", "on", "at", "to", "for", "by", "with", "from", "as",
          "vs", "via", "per", "into", "onto"}
_UNITS = {"kg", "km", "cm", "mm", "ml", "mg", "g", "m", "kw", "mw"}
_HAS = re.compile(r"[^\W_]" + _D + r"|" + _D + r"\s*[^\W_]")


def is_title_case(text: str) -> bool:
    words = re.findall(r"[^\W\d_]{4,}", text)
    return len(words) < 2 or sum(1 for w in words if w[0].isupper()) / len(words) >= 0.6


def no_hyphens(text: str, case: bool | None = None) -> str:
    """`text` without hyphens joining words. `case`: capitalise the word after a removed hyphen (default: when the text is in Title Case)."""
    t = text or ""
    if not _HAS.search(t):
        return t
    if case is None:
        case = is_title_case(t)
    t = re.sub(rf"(?<![^\W_.])(\d{{4}})\s*{_D}\s*(\d{{4}})(?![^\W_])", r"\1 to \2", t)
    t = re.sub(rf"(?<![^\W_.])(\d{{4}})\s*{_D}\s*(\d{{2}})(?![^\W_])", lambda m: f"{m[1]} to {m[1][:2]}{m[2]}", t)
    t = re.sub(rf"(?<![^\W_.])(\d+(?:\.\d+)?)\s*{_D}\s*(\d+(?:\.\d+)?)(?![^\W_])", r"\1 to \2", t)
    if case:
        def up(m: re.Match) -> str:
            w = m[1]
            return "-" + (w if w in _SMALL or w in _UNITS else w[0].upper() + w[1:])
        t = re.sub(rf"(?<=[^\W_]){_D}([^\W\d_][^\W_]*)", lambda m: up(m) if m[1][0].islower() else m[0], t)
    t = re.sub(rf"(?<=[^\W_]){_D}\s*(?=[^\W_])|(?<=[^\W\d_]){_D}\s+(?=[^\W\d_])", " ", t)
    return re.sub(r"\s+", " ", t).strip()


_SEP = re.compile(r"\s+[|\-\u2013\u2014]\s+(?=[^|\-\u2013\u2014]+$)")


def _key(name: str) -> str:
    n = re.sub(r"^https?://|^www\.|\.(com|in|org|net|co\.in|gov\.in)(/.*)?$", "", (name or "").strip().lower())
    return re.sub(r"[^a-z0-9]", "", n)


def no_publisher(title: str, *names: str) -> str:
    """`title` without the publisher a feed appends after a separator ("... | Hindustan Times", "... - The Hindu",
    "... \u2014 TOI"). Only a trailing part that is one of `names` (publisher, source name or host) goes; a colon tail
    ("...: Delhi High Court") and other trailing words stay (owner, 18 Oct 2026)."""
    t = (title or "").strip()
    bare = lambda k: k[3:] if k.startswith("the") and len(k) > 6 else k  # "The Hindu" = "thehindu.com"
    keys = {bare(k) for k in (_key(n) for n in names) if len(k) >= 3}
    while keys:
        m = _SEP.search(t)
        if not m or bare(_key(t[m.end():])) not in keys:
            break
        t = t[: m.start()].rstrip()
    return t


# A section or channel label a publisher appends after " | " or " - " (owner, 20 Oct 2026): "| India News",
# "| Mumbai news", "| Watch", "| Videos", "| Trending", "- Blog", "| SCC Times", "| Akashvani News". Only these labels
# go, and only at the end; a real headline part ("- go vegan", "| World Farm Animals Day 2026") stays.
_SECTION = re.compile(r"\s+[|\-\u2013\u2014]\s+(?:(?:India|World|City|Mumbai|Delhi|Bengaluru|Kolkata|Chennai|Hyderabad|Pune|"
                      r"Lucknow|Latest|Breaking|Akashvani)\s+news|news|watch|videos?|trending|blogs?|photos?|scc times)\s*$", re.I)


_PIPE = re.compile(r"\s+\|\s+(?=[^|]+$)")


def no_section(title: str, *names: str) -> str:
    """`title` without trailing section labels and publisher names, in any order ("... | Trending | Hindustan Times").
    After " | ", a trailing part that starts with a publisher's name also goes ("| DIPR Nagaland Department of
    Information ..."); after a dash it stays ("— PETA India campaign targets Gen Z" is part of the headline)."""
    t = (title or "").strip()
    keys = [k for k in (_key(n) for n in names) if len(k) >= 4]
    while True:
        before = t
        t = no_publisher(_SECTION.sub("", t).rstrip(), *names)
        m = _PIPE.search(t)
        if m and keys and any(_key(t[m.end():]).startswith(k) for k in keys) and m.start() > 20:
            t = t[: m.start()].rstrip()
        if t == before:
            return t


_CUT = re.compile(r"(?:\u2026|\.\.\.)\s*$")


def is_cut(title: str) -> bool:
    """True when a feed cut the headline off with "…" or "..."."""
    return bool(_CUT.search(title or ""))


def _fold(text: str) -> str:
    t = re.sub(r"[\u2018\u2019\u201c\u201d\"'`]", "", (text or "").lower())
    t = re.sub(_D, " ", t)
    return re.sub(r"[^\w&₹]+", " ", t).strip()


def completes(cut: str, full: str) -> bool:
    """True when `full` is the headline that `cut` ("Bombay High Court threatens ... stray dog, ...") was cut from:
    it starts with the same words (ignoring case, quotes, hyphens), is longer, and has no "…" itself."""
    head = _fold(_CUT.sub("", cut or "").rstrip(" ,;:-"))
    head = head.rsplit(" ", 1)[0] if " " in head else head  # the last word may be cut mid-word ("offer c...")
    body = _fold(full)
    cut_too = "\u2026" in (full or "") or "..." in (full or "")  # "… | UNI India": the page cut it as well
    return bool(head) and len(head) >= 12 and body.startswith(head) and len(body) > len(head) + 2 and not cut_too
