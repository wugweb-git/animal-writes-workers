"""Relevance gate (owner, 10 Oct 2026): one check every pipeline runs before it creates anything on the site.

An item reaches the gate only after the cheap filters (sourcing policy, animal words, duplicates). A cheap model,
through the same providers and keys as the summaries, reads it and answers in JSON:
    {"relevant": "yes|no", "category": "<topic slug>", "india": "yes|no", "issuer": "...", "reason": "..."}
It passes when it is relevant, fits one of the site's Categories (aw_topic), concerns India, and — for Library and
Resources documents — was issued by a known body (courts, AWBI, ministries and departments, Gazette, PIB, NGOs in the
Directory). Library items issued by an Indian body count as India.

Rejected items are not created; their title, URL and reason go to the site's "Rejected" log (Inbox → To review).
When every provider fails, nothing is created and the item is not marked seen, so the next run tries again.
Verdicts are cached by URL (.state/relevance.json).
"""

from __future__ import annotations

import json
import logging
import os
import re
from urllib.parse import urlparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from .summarise import MODEL as HAIKU_MODEL, _is_dead_key_error, note_failure, note_success, usable, wait_for_cooldown  # noqa: F401

log = logging.getLogger("aw_workers.relevance")

CACHE_FILE = Path(os.environ.get("AW_RELEVANCE_CACHE", ".state/relevance.json"))
UNAVAILABLE = "classifier unavailable"

# The site's Categories (wp term list aw_topic, 10 Oct 2026); the site's own list replaces this when it is reachable.
CATEGORIES = {
    "adoption": "Adoption",
    "animal-birth-control": "Animal Birth Control",
    "transport-slaughter": "Animal Transport and Slaughter",
    "birds": "Birds",
    "campaigns-civic-action": "Campaigns and Civic Action",
    "cattle-gaushalas": "Cattle and Gaushalas",
    "community-dogs": "Community Dogs",
    "courts-judgments": "Courts and Judgments",
    "cruelty-prosecution": "Cruelty and Prosecution",
    "disaster-rescue": "Disaster and Emergency Rescue",
    "feeders-caregivers": "Feeders and Caregivers",
    "laws-rules": "Laws and Rules",
    "performing-captive-animals": "Performing and Captive Animals",
    "pets-breeding": "Pets, Breeding and Pet Shops",
    "policy-government": "Policy and Government",
    "rabies-public-health": "Rabies and Public Health",
    "veterinary-care": "Veterinary Care",
    "wildlife": "Wildlife",
}

# Issuers the site already knows (issuers.php slugs) that count as a known body.
KNOWN_ISSUER_SLUGS = {
    "supreme-court", "high-court", "national-green-tribunal", "awbi", "central-ministry", "state-government",
    "municipal-body", "parliament", "gazette", "pib",
}
KNOWN_ISSUER = re.compile(
    r"animal welfare board|\bawbi\b|ministry of environment|\bmoef(?:cc)?\b|fisheries,? animal husbandry|\bmofah ?& ?d\b|"
    r"\bdahd\b|animal husbandry|forest department|department of forests?|forests? (?:and|&) (?:wildlife|environment)|"
    r"wildlife department|chief wildlife warden|supreme court|high court|national green tribunal|\bngt\b|gazette|"
    r"press information bureau|\bpib\b|government of india|central government|state government|government of [a-z ]+|"
    r"\bministry\b|parliament|lok sabha|rajya sabha|municipal|nagar nigam|corporation of|central zoo authority|"
    r"wildlife crime control bureau|national tiger conservation|law commission|district magistrate|collector|"
    r"\bcourt\b|tribunal|consumer (?:commission|forum)|\bcommission\b|magistrate|\bbench\b",
    re.I,
)

INDIAN_PUBLISHER = re.compile(
    r"times of india|timesofindia|hindustan times|hindustantimes|the hindu|thehindu|indian express|indianexpress|ndtv|"
    r"india today|indiatoday|news18|deccan|the print|theprint|scroll\.in|the wire|thewire|livemint|mint|firstpost|"
    r"tribune|telegraph india|telegraphindia|the quint|thequint|dna india|dnaindia|zee news|zeenews|abp|aaj tak|"
    r"economic times|economictimes|business standard|business-standard|outlook|india tv|indiatv|dt next|dtnext|"
    r"mathrubhumi|manorama|siasat|free press journal|freepressjournal|mid-day|midday|lokmat|dainik|jagran|"
    r"amar ujala|amarujala|bhaskar|navbharat|pti|ians|ani\b|aninews|live ?law|livelaw|bar (?:and|&) bench|barandbench|"
    r"scc ?online|pib|petaindia|peta india|down to earth|downtoearth|mongabay india|india\.mongabay",
    re.I,
)

PROMPT = """You check items for Animal Writes, a website about animal welfare in India. Decide if this item belongs there.

Categories (slug: name):
{categories}

Rules:
- relevant is "yes" only if the CONTENT is about animal welfare (how animals are treated, protected, rescued, cared for or
  harmed, or a law, court case, policy or public-health matter about animals) AND it fits one of the categories.
  An animal word in the title is not enough.
- relevant is "no" for: sports, teams or players (e.g. "Tigers", "Bulls"); finance ("bull market", "bear market");
  film, TV, music or entertainment; food, recipes or restaurants; livestock, meat, dairy or fish trade prices and markets;
  general politics or crime with no animal-welfare angle; generic documents not issued by an animal-welfare body,
  a court or a government body.
- category: the one best slug from the list ("" when not relevant).
- india is "yes" when the item concerns India, an Indian state, district or city, or an Indian body (court, ministry,
  board, department, Indian NGO).
- issuer: for a document, judgment, notification or paper, the body that issued or published it; otherwise "".
- reason: one short sentence.
- Item types: news (a news report), legal (a Library document or court/legal report), research (a research paper),
  resource (a guide or document), opportunity (a job, volunteering or grant at an organisation), press (coverage of
  Animal Writes or its 29 November 2025 letter campaign about community dogs: relevant when it is about that campaign).

Item type: {kind}
Source: {source}
URL: {url}
Title: {title}
Text: {text}

Answer with JSON only:
{{"relevant": "yes|no", "category": "<slug>", "india": "yes|no", "issuer": "<body or empty>", "reason": "<one sentence>"}}"""


@dataclass
class Candidate:
    kind: str  # news | legal | research | resource | opportunity | press
    title: str
    url: str
    text: str = ""
    source: str = ""
    issuer: str = ""  # issuer already known from the source (court, AWBI, ...), when there is one
    feed: str = ""


@dataclass
class Verdict:
    relevant: bool
    category: str
    india: bool
    issuer: str
    reason: str
    model: str
    at: str

    def to_json(self) -> dict:
        return asdict(self)


def _yes(v) -> bool:
    return v is True or str(v).strip().lower() in ("yes", "true", "1", "y")


def parse(reply: str) -> dict | None:
    m = re.search(r"\{.*\}", reply or "", re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict) or "relevant" not in data:
        return None
    return data


def backend_label(b) -> str:
    return f"{b.name}:{getattr(b, 'model', HAIKU_MODEL)}"


def norm_name(s: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", (s or "").lower()).split())


class Gate:
    """Shared by run, backfill, research, press and opportunities. `summariser` supplies the providers (and their
    dead-key list); None or a dry-run summariser means "not checked" (dry runs spend nothing)."""

    def __init__(self, summariser=None, *, client=None, categories: dict | None = None, directory: list[str] | None = None,
                 cache: dict | None = None, dry_run: bool = False):
        self.summariser = summariser
        self.backends = list(getattr(summariser, "backends", []) or [])
        self.dead = getattr(summariser, "dead", set())
        self.cool = summariser.__dict__.setdefault("cool", {}) if summariser is not None and hasattr(summariser, "__dict__") else {}  # resting keys, shared
        self.client = client
        self.categories = dict(categories or CATEGORIES)
        self.directory = {norm_name(n) for n in (directory or []) if norm_name(n)}
        self.cache = cache if cache is not None else {}
        self.dry_run = dry_run
        self.counts = {"pass": 0, "reject": 0, "unavailable": 0, "cached": 0}

    @classmethod
    def from_site(cls, summariser, client, *, dry_run: bool = False, cache_file: Path = CACHE_FILE) -> "Gate":
        """Categories and Directory names from the site when reachable; the built-in list otherwise."""
        cats, directory = None, None
        if client is not None and hasattr(client, "relevance_config"):
            try:
                cfg = client.relevance_config()
                cats = cfg.get("categories") or None
                directory = cfg.get("directory") or None
            except Exception as exc:
                log.warning("relevance config unavailable, using the built-in categories: %s", exc)
        return cls(summariser, client=client, categories=cats, directory=directory, cache=load_cache(cache_file), dry_run=dry_run)

    # ---- the model ----

    def classify(self, c: Candidate) -> Verdict | None:
        if c.url and c.url in self.cache:
            self.counts["cached"] += 1
            return Verdict(**self.cache[c.url])
        prompt = PROMPT.format(
            categories="\n".join(f"{k}: {v}" for k, v in self.categories.items()),
            kind=c.kind, source=c.source or "unknown", url=c.url, title=c.title,
            text=re.sub(r"\s+", " ", c.text or "")[:1500] or "(no further text)",
        )
        for _ in range(2):  # a second pass once a rate-limited key has rested
            for i, backend in enumerate(self.backends):
                if not usable(self, i):
                    continue
                try:
                    data = parse(backend.ask(prompt))
                except Exception as exc:
                    note_failure(self, i, exc)
                    log.info("relevance: %s failed: %s", backend.name, str(exc)[:150])
                    continue
                note_success(self, i)
                if data is None:
                    continue
                v = Verdict(
                    relevant=_yes(data.get("relevant")), category=str(data.get("category") or "").strip(),
                    india=_yes(data.get("india")), issuer=str(data.get("issuer") or "").strip()[:200],
                    reason=str(data.get("reason") or "").strip()[:300], model=backend_label(backend),
                    at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                )
                if c.url:
                    self.cache[c.url] = v.to_json()
                return v
            if not wait_for_cooldown(self):
                break
        return None

    # ---- the rules ----

    def category_slug(self, value: str) -> str:
        v = (value or "").strip().lower()
        for slug, name in self.categories.items():
            if v in (slug.lower(), name.lower()):
                return slug
        return ""

    def known_issuer(self, *names: str) -> bool:
        for n in names:
            if not n:
                continue
            if n in KNOWN_ISSUER_SLUGS or KNOWN_ISSUER.search(n) or norm_name(n) in self.directory:
                return True
        return False

    @staticmethod
    def looks_indian(c: Candidate) -> bool:
        """A second opinion on India, because the model sometimes says "not about India" for Indian stories (12 Oct 2026):
        an Indian place or body in the title/text, an Indian publisher, or an .in address."""
        from .research import INDIA

        host = (urlparse(c.url).hostname or "").lower()
        hay = f"{c.title} {c.text[:1500]} {c.source}"
        return bool(INDIA.search(hay) or host.endswith(".in") or INDIAN_PUBLISHER.search(f"{c.source} {host}"))

    def decide(self, c: Candidate, v: Verdict) -> tuple[bool, str]:
        """(passes, reason). Documents (Library; Resources other than research papers and own guides) need a known
        issuer; a Library item issued by an Indian body counts as India. A category is not required: the site assigns
        topics itself, and opportunities/press have none (false rejections, 12 Oct 2026)."""
        if not v.relevant:
            return False, v.reason or "not about animal welfare"
        doc = c.kind in ("legal", "resource")
        issuer_ok = self.known_issuer(c.issuer, v.issuer, c.title, c.text[:600]) if doc else True
        if doc and not issuer_ok:
            return False, f"issuer not a known body ({v.issuer or c.issuer or 'unknown'})"
        if c.kind in ("opportunity", "press"):
            return True, v.reason  # Indian organisations / coverage of Animal Writes: India is a given
        if not v.india and not (c.kind == "legal" and issuer_ok) and not self.looks_indian(c):
            return False, f"not about India: {v.reason}"
        return True, v.reason

    # ---- the one call every pipeline makes ----

    def admit(self, c: Candidate) -> tuple[str, Verdict | None]:
        """'pass', 'reject' or 'unavailable'. Rejections are logged on the site; nothing is created for them."""
        if not self.backends:
            if self.dry_run:
                return "pass", None  # dry run without spending: not checked
            self.counts["unavailable"] += 1
            return "unavailable", None
        v = self.classify(c)
        if v is None:
            self.counts["unavailable"] += 1
            log.warning("relevance: %s for %s", UNAVAILABLE, c.url)
            self._log_rejected(c, UNAVAILABLE)
            return "unavailable", None
        ok, why = self.decide(c, v)
        if ok:
            self.counts["pass"] += 1
            return "pass", v
        self.counts["reject"] += 1
        log.info("relevance: rejected %s (%s)", c.url, why)
        self._log_rejected(c, why, v)
        return "reject", v

    def _log_rejected(self, c: Candidate, reason: str, v: Verdict | None = None) -> None:
        if self.dry_run or self.client is None or not hasattr(self.client, "reject"):
            return
        try:
            self.client.reject({"title": c.title[:300], "source_url": c.url, "reason": reason[:300], "type": c.kind,
                                "feed": c.feed, "model": v.model if v else ""})
        except Exception as exc:  # the log is a convenience; never stop the run for it
            log.warning("relevance: could not log rejection of %s: %s", c.url, exc)

    def save(self, path: Path = CACHE_FILE) -> None:
        save_cache(path, self.cache)


def load_cache(path: Path = CACHE_FILE) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_cache(path: Path, cache: dict, keep: int = 20000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    items = list(cache.items())[-keep:]
    path.write_text(json.dumps(dict(items), ensure_ascii=False))


def gate_from_env(client, *, dry_run: bool, env=os.environ, summariser=None) -> Gate:
    """A gate for one-off jobs (research, press searches): the summary providers from the environment."""
    from . import summarise

    if summariser is None and not dry_run:  # dry runs spend nothing: not checked
        backends = summarise.backends_from_env(env, ledger={})
        summariser = summarise.Summariser(backends) if backends else None
    return Gate.from_site(summariser, client, dry_run=dry_run)


def candidate_for(item, payload_issuer: str = "") -> Candidate:
    """A sourced Item (run, backfill, research, opportunities) as a gate candidate."""
    kind = item.source.type
    from .ingest import court_type

    st = court_type(item.title) if item.source.source_type == "auto-court" else item.source.source_type
    issuer = payload_issuer or item.court or st or (item.source.org if kind == "opportunity" else "")
    if item.source.kind == "awbi":
        issuer = issuer or "awbi"
    return Candidate(kind=kind, title=item.title, url=item.url, text=item.text,
                     source=item.publisher or item.source.name, issuer=issuer, feed=item.source.id)
