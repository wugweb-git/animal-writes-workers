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
