"""The one rule set for summaries (owner, 15 Oct 2026).

The rules live on the site (Settings → Sourcing → Summary rules, option aw_summary_rules) and are served at
aw/v1/summary-rules. Every summary writer here builds its prompt and its checks from them: new summaries
(summarise.py), same-story rewrites (merge.py) and the clean-up tools that use them (fold_existing.py). When the site
cannot be reached, the same defaults are used (summary_rules.json, a copy of the site's data/summary-rules-default.json).

Sections: news (News), library (Library: court orders, Acts, Rules, circulars, releases), research (Resources research).
A summary is never shorter than the page's search description: each section's minimum is well above the 155 characters
of the description, which is the summary's lead sentence (titles.php in aw-seo-extras).
"""

from __future__ import annotations

import copy
import json
import logging
import re
from pathlib import Path

log = logging.getLogger("aw_workers.summary_rules")

DEFAULTS: dict = json.loads((Path(__file__).with_name("summary_rules.json")).read_text(encoding="utf-8"))
_current: dict | None = None

SECTION_OF_TYPE = {"news": "news", "legal": "library", "research": "research",
                   "aw_news": "news", "aw_legal": "library", "post": "research"}


def merged(site: dict | None) -> dict:
    """The site's rules over the defaults: a missing or unusable value falls back to the default."""
    out = copy.deepcopy(DEFAULTS)
    if not isinstance(site, dict):
        return out
    for k, v in site.items():
        if k == "sections" and isinstance(v, dict):
            for name, sec in v.items():
                if isinstance(sec, dict):
                    base = out["sections"].setdefault(name, {})
                    for f, val in sec.items():
                        if f in ("min_words", "max_words", "min_sentences", "max_sentences"):
                            try:
                                val = int(val)
                            except (TypeError, ValueError):
                                continue
                        base[f] = val
        elif k in DEFAULTS and type(v) is type(DEFAULTS[k]) and v not in ("", []):
            out[k] = v
    for sec in out["sections"].values():  # never a minimum above the maximum
        sec["min_words"] = min(int(sec.get("min_words", 0)), int(sec.get("max_words", 10**6)))
    return out


def use(client) -> dict:
    """Read the rules from the site once per run (falls back to the defaults); later calls to current() return them."""
    global _current
    site = None
    if client is not None and hasattr(client, "summary_rules"):
        try:
            site = client.summary_rules()
        except Exception as exc:  # noqa: BLE001
            log.warning("summary rules not read from the site, using the defaults: %s", type(exc).__name__)
    _current = merged(site)
    log.info("summary rules: %s", "from the site" if site else "defaults")
    return _current


def current() -> dict:
    return _current if _current is not None else merged(None)


def reset() -> None:
    global _current
    _current = None


def section_for(kind: str) -> str:
    return SECTION_OF_TYPE.get(kind or "", "news")


# ---- text measures ----

def words(text: str) -> list[str]:
    return re.findall(r"[\w'’-]+", text or "")


def sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+(?=[\"“‘(]?[A-Z0-9])", (text or "").strip()) if s]


def _phrase_re(phrase: str) -> re.Pattern:
    return re.compile(r"(?<![\w-])" + re.escape(phrase.strip()).replace(r"\ ", r"\s+") + r"(?![\w-])", re.I)


def _lines(values) -> list[list[str]]:
    """'canonical | alias | alias' lines -> [[canonical, alias, ...], ...]."""
    out = []
    for line in values or []:
        parts = [p.strip() for p in str(line).split("|") if p.strip()]
        if parts:
            out.append(parts)
    return out


def quotes(text: str) -> list[str]:
    return re.findall(r"[\"“”]([^\"“”]{3,})[\"“”]", text or "")


def outside_quotes(text: str) -> str:
    return re.sub(r"[\"“][^\"“”]{3,}[\"”]", " ", text or "")


def longest_copied_run(summary: str, source_text: str) -> int:
    a, b = [w.lower() for w in words(summary)], [w.lower() for w in words(source_text)]
    if not a or not b:
        return 0
    best, prev = 0, [0] * (len(b) + 1)
    for x in a:
        cur = [0] * (len(b) + 1)
        for j, y in enumerate(b, 1):
            if x == y:
                cur[j] = prev[j - 1] + 1
                best = max(best, cur[j])
        prev = cur
    return best


def _exact_names(rules: dict) -> list[str]:
    """Canonical entity names: copying them word for word is naming, not copying."""
    return [parts[0] for parts in _lines(rules.get("entities"))]


def entities_in(text: str, rules: dict | None = None) -> list[str]:
    """Canonical names of the entities (laws, bodies, organisations) that `text` names by any of their forms."""
    rules = rules or current()
    found = []
    for parts in _lines(rules.get("entities")):
        if any(_phrase_re(p).search(text or "") for p in parts):
            found.append(parts[0])
    return found


# ---- checks ----

SHORT = "short:"  # problems that only say "too short": the fallback (re-read the source, or hold for review) applies


def problems(summary: str, section: str, source_text: str = "", rules: dict | None = None,
             publisher: str = "") -> list[str]:
    """Why `summary` breaks the rules ([] = it follows them). A problem starting with SHORT only says it is below a
    minimum; those are never fixed by padding."""
    rules = rules or current()
    sec = rules["sections"].get(section) or rules["sections"]["news"]
    out: list[str] = []
    text = summary or ""
    n_words, n_sent = len(words(text)), len(sentences(text))
    if n_words < sec["min_words"]:
        out.append(f"{SHORT} needs at least {sec['min_words']} words, got {n_words} (never pad: only facts from the source)")
    if n_words > sec["max_words"]:
        out.append(f"over {sec['max_words']} words ({n_words})")
    if n_sent < sec["min_sentences"]:
        out.append(f"{SHORT} needs at least {sec['min_sentences']} sentences, got {n_sent}")
    if n_sent > sec["max_sentences"]:
        out.append(f"needs at most {sec['max_sentences']} sentences, got {n_sent}")
    lead = sentences(text)[:1]
    if lead and len(words(lead[0])) > int(rules.get("lead_max_words", 30)) + 10:
        out.append(f"the lead sentence is {len(words(lead[0]))} words: it must stand alone as the search description "
                   f"(about {rules.get('lead_max_words', 30)} words)")
    qs = quotes(text)
    if len(qs) > int(rules.get("max_quotes", 1)):
        out.append(f"more than {rules.get('max_quotes', 1)} direct quote")
    if any(len(words(q)) > int(rules.get("max_quote_words", 25)) for q in qs):
        out.append(f"a quote longer than {rules.get('max_quote_words', 25)} words")
    if source_text:
        own = outside_quotes(text)
        for name in _exact_names(rules):
            own = _phrase_re(name).sub(" ", own)
        run = longest_copied_run(own, source_text)
        if run > int(rules.get("max_copied_run", 20)):
            out.append(f"copies {run} consecutive words from the source outside a quote (at most {rules.get('max_copied_run', 20)})")
    low = text.lower()
    stock = sorted({p.lower() for p in rules.get("stock_phrases") or [] if _phrase_re(p).search(text)})
    if stock:
        out.append("stock or praise phrases: " + ", ".join(stock))
    ai = sorted({m for m in (x.lower() for x in rules.get("ai_markers") or []) if m in low})
    if ai:
        out.append("reads as generated text: " + ", ".join(ai))
    if re.search(r"[ऀ-෿]", text):
        out.append("Indian-language script in the summary: English only")
    if re.search(r"<[a-z/][^>]*>|https?://|www\.|\[[^\]]+\]\([^)]+\)|\*\*|(^|\n)\s*(#|[-*•]\s)|\n", text, re.I):
        out.append("plain text only: no link, URL, HTML, heading, list, bold or line break")
    if re.search(r"\b(click here|read more|here\s*:)", low):
        out.append("no 'click here' or 'read more': actions are buttons on the page")
    if publisher and publisher.strip() and section == "news":
        pub = re.escape(publisher.strip())
        if re.search(r"according to\s+(?:a |the )?(?:reports? (?:by|in)\s+)?" + pub + r"|" + pub
                     + r"\s+(?:said|says|reported|reports|stated)", text, re.I):
            out.append("names the publisher as the source of the story (the source row credits it)")
    out += searched_word_problems(text, (source_text or "")[:400], rules)  # the title and the opening: what it is about
    out += entity_problems(text, rules)
    return out


def searched_word_problems(summary: str, source_text: str, rules: dict | None = None) -> list[str]:
    """A searched word in the source (stray dogs, gaushala, NGO) that the summary replaced with a softer word."""
    rules = rules or current()
    out = []
    for group in _lines(rules.get("searched_terms")):
        if any(_phrase_re(t).search(source_text or "") for t in group) and not any(_phrase_re(t).search(summary or "") for t in group):
            out.append(f"keep the searched words: the source says '{next(t for t in group if _phrase_re(t).search(source_text))}'")
    return out


def entity_problems(summary: str, rules: dict | None = None) -> list[str]:
    """An entity written by an alias instead of the site's name (one form per entity, so it can be linked)."""
    rules = rules or current()
    out = []
    for parts in _lines(rules.get("entities")):
        canon, aliases = parts[0], parts[1:]
        rest = _phrase_re(canon).sub(" ", summary or "")
        used = [a for a in aliases if (re.search(r"(?<![\w-])" + re.escape(a) + r"(?![\w-])", rest) if a.isupper()
                                       else _phrase_re(a).search(rest))]
        if used:
            out.append(f"write '{canon}', not '{used[0]}' (the site's name, so it can be linked)")
    return out


def only_short(found: list[str]) -> bool:
    return bool(found) and all(p.startswith(SHORT) for p in found)


# ---- prompts ----

def rules_block(section: str, rules: dict | None = None, source_text: str = "") -> str:
    """The rules as the prompt states them, for one section."""
    rules = rules or current()
    sec = rules["sections"].get(section) or rules["sections"]["news"]
    lines = [
        f"Length: {sec['min_words']} to {sec['max_words']} words, {sec['min_sentences']} to {sec['max_sentences']} sentences. "
        f"The lead sentence about {rules.get('lead_max_words', 30)} words or fewer.",
        "Structure: " + sec.get("structure", ""),
        "Attribution: " + sec.get("attribution", ""),
        f"Quotes: at most {rules.get('max_quotes', 1)} direct quote of at most {rules.get('max_quote_words', 25)} words; "
        f"never copy more than {rules.get('max_copied_run', 20)} words in a row outside a quote.",
    ]
    lines += list(rules.get("general") or [])
    stock = rules.get("stock_phrases") or []
    if stock:
        lines.append("Never use these words or phrases: " + ", ".join(f'"{s}"' for s in stock) + ".")
    named = entities_in(source_text, rules) if source_text else []
    if named:
        lines.append("Name these exactly so: " + "; ".join(named) + ".")
    return "\n".join(f"- {line}" for line in lines if line.strip())
