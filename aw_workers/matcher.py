"""One keyword matcher, the same on the site and in the workers (sourcing restructure, phase 1, owner 25 Oct 2026).

Word mode (the site sends `match: "word"`, from phase 2):
- Latin words and phrases match whole words, with a plural allowed ("dog" matches "dogs", never "dogma").
- A trailing `*` matches the start of a word ("steriliz*" matches "sterilized" and "sterilization").
- Other scripts (Hindi etc.) match as substrings, because they have no word boundaries.

Legacy mode (the default until the site sends `match: "word"`): each caller keeps the matching it had before.

The cases in tests/fixtures/match_cases.json are tested in both repositories (the site copy sits in
plugins/aw-sourcing/tests/fixtures/match_cases.json); change both together.
"""

from __future__ import annotations

import re

WORD = "word"
LEGACY = "legacy"

_state = {"mode": LEGACY}


def set_mode(mode) -> None:
    """The site's `match` value; anything but "word" keeps the legacy matching."""
    _state["mode"] = WORD if str(mode or "").strip().lower() == WORD else LEGACY


def mode() -> str:
    return _state["mode"]


def word_mode() -> bool:
    return _state["mode"] == WORD


def matches(hay: str, term: str) -> bool:
    """Word-mode match of one keyword in a text (case-insensitive)."""
    t = " ".join(str(term or "").lower().split())
    if not t:
        return False
    hay = (hay or "").lower()
    prefix = t.endswith("*")
    if prefix:
        t = t.rstrip("*").rstrip()
        if not t:
            return False
    if not t.isascii():
        return t in hay  # other scripts have no word boundaries
    body = r"\s+".join(re.escape(p) for p in t.split(" "))
    if prefix:
        return re.search(r"(?<![a-z0-9])" + body, hay) is not None
    return re.search(r"(?<![a-z0-9])" + body + r"(?:s|es)?(?![a-z0-9])", hay) is not None


def any_match(hay: str, terms) -> str:
    """The first term that matches in word mode, or ""."""
    for t in terms or []:
        if matches(hay, t):
            return str(t)
    return ""
