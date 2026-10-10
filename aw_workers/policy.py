"""Sourcing policy managed in WP Admin (News → Sourcing), read before each run.

Mirrors aw_sourcing_gate() in animal-writes-core so items the site would refuse are skipped before they are
summarised. The site checks again on arrival, so a stale policy can only cost a summary, never let an item in.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

AGGREGATOR = re.compile(r"^(gnews|applenews|agg)-")

# Minimal guard (SEO audit 10 Oct 2026): News and Library items must name an animal somewhere in title + feed text,
# whatever the site's relevance switch or allowed-publisher list says. Whole words; the site's strong/animal terms count
# too. Phrases where "stray" or "bull" is not an animal are removed first ("stray vacancy round", "bull market").
ANIMAL_WORDS = re.compile(
    r"\b(animals?|dogs?|pupp(?:y|ies)|cats?|kittens?|cattle|cows?|calf|calves|bulls?|bullocks?|oxen|buffalo(?:es)?|goats?|"
    r"sheep|pigs?|horses?|ponies|pony|mules?|donkeys?|camels?|elephants?|tigers?|tigress|leopards?|lions?|cheetahs?|birds?|"
    r"wildlife|wild life|strays?|pets?|rabies|rabid|vets?|veterinar\w*|monkeys?|macaques?|langurs?|bears?|snakes?|"
    r"reptiles?|turtles?|tortoises?|deer|fish|fisheries|poultry|chickens?|chicks?|hens?|meat|slaughter\w*|zoos?|"
    r"zoological|species|gau ?shalas?|go ?shalas?|gaushala|bovine|crocodiles?|peacocks?|parrots?|owls?|wolf|wolves|"
    r"jackals?|foxes|fox|rhinos?|pythons?|pangolins?|dolphins?|whales?|sharks?|beef|dairy|leather|fur|ivory|poach\w*|"
    r"hunting|poacher|feeders?|kennels?|canine|feline|livestock|fodder|peta|awbi|zoonotic|zoonoses|lumpy skin|"
    r"bird flu|avian|swine|vultures?|eagles?|hyenas?|bison|gaur|squirrels?|frogs?|bats?|shrimps?|crabs?|insects?|bees?|"
    r"sanctuary|national park|tiger reserve|mammals?|creatures?|fauna|vegan|cruelty|geese|goose|gibbons?|foie gras|"
    r"bestiality|cockfight\w*|bullfight\w*|jallikattu|kambala|biodiversity|habitat)\b",
    re.I,
)
NOT_ANIMAL = re.compile(
    r"\bstray (?:vacanc\w*|bullets?|rounds?|remarks?|incidents?|cases?|votes?|seats?|counsell?ing)\b|\bbull (?:market|run)\b|"
    r"\bbear market\b|\bpet (?:project|peeve)s?\b|\bcat ?scan\b",
    re.I,
)
ANIMAL_WORDS_HI = ("पशु", "जानवर", "कुत्त", "गाय", "गौ", "रेबीज", "बंदर", "तेंदुआ", "हाथी", "बाघ", "पक्षी", "मवेशी")


def about_animals(text: str, policy: "Policy | None" = None) -> bool:
    """True when the text names an animal (built-in words, Hindi words, or the site's strong/animal terms)."""
    hay = NOT_ANIMAL.sub(" ", text or "")
    if ANIMAL_WORDS.search(hay) or any(w in hay for w in ANIMAL_WORDS_HI):
        return True
    if policy is not None:
        low = hay.lower()
        return any(has_term(low, t) for t in [*policy.strong, *policy.animal])
    return False


def norm_title(title: str) -> str:
    """Title for duplicate checks: lower case, letters and digits only, single spaces; a trailing source name
    ("... - India Today", "India News | ...") does not count."""
    t = (title or "").lower()
    t = re.sub(r"\s+[-–—|]\s+[^-–—|]{2,40}$", "", t)  # " - Publisher" at the end
    t = re.sub(r"^[^|]{2,30}\|\s+", "", t)  # "India News | " at the start
    return " ".join(re.sub(r"[^\w\s]|_", " ", t).split())


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
