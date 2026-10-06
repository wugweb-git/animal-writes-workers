"""Sourcing policy managed in WP Admin (News → Sourcing), read before each run.

Mirrors aw_sourcing_gate() in animal-writes-core so items the site would refuse are skipped before they are
summarised. The site checks again on arrival, so a stale policy can only cost a summary, never let an item in.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

AGGREGATOR = re.compile(r"^(gnews|applenews|agg)-")


def host(url: str) -> str:
    h = (urlparse(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def has_term(hay: str, term: str) -> bool:
    t = term.strip().lower()
    if not t:
        return False
    if re.fullmatch(r"[a-z0-9 \-]{1,5}", t):
        return re.search(r"\b" + re.escape(t) + r"\b", hay) is not None
    return t in hay


@dataclass
class Policy:
    blocked: set[str] = field(default_factory=set)
    allowed: set[str] = field(default_factory=set)
    aggregator_allow_only: bool = False
    relevance_on: bool = False
    strong: list[str] = field(default_factory=list)
    animal: list[str] = field(default_factory=list)
    context: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)

    @classmethod
    def from_json(cls, data: dict) -> "Policy":
        pub = data.get("publishers") or {}
        rel = data.get("relevance") or {}
        return cls(
            blocked=set(pub.get("blocked") or []),
            allowed=set(pub.get("allowed") or []),
            aggregator_allow_only=bool(pub.get("gnews_allow_only")),
            relevance_on=bool(rel.get("enabled")),
            strong=list(rel.get("strong") or []),
            animal=list(rel.get("animal") or []),
            context=list(rel.get("context") or []),
            exclude=list(rel.get("exclude") or []),
        )

    def relevant(self, text: str) -> tuple[bool, str]:
        hay = text.lower()
        for t in self.exclude:
            if has_term(hay, t):
                return False, f"exclude term: {t}"
        for t in self.strong:
            if has_term(hay, t):
                return True, f"strong term: {t}"
        a = next((t for t in self.animal if has_term(hay, t)), None)
        if a:
            c = next((t for t in self.context if has_term(hay, t)), None)
            if c:
                return True, f"animal term: {a} + context term: {c}"
            return False, f"animal term ({a}) without a context term"
        return False, "no animal or strong term"

    def verdict(self, url: str, feed: str, text: str) -> str:
        """'' to keep, otherwise 'blocked' or 'filtered' (same rules as the site)."""
        h = host(url)
        if h in self.blocked:
            return "blocked"
        if self.aggregator_allow_only and AGGREGATOR.match(feed) and h not in self.allowed:
            return "blocked"
        if self.relevance_on and h not in self.allowed and not self.relevant(text)[0]:
            return "filtered"
        return ""
