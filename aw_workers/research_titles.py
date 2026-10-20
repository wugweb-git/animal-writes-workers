"""Research titles (owner, 20 Oct 2026): three titles per research item, kept apart.

- Study title: the paper's exact title (doi.org CSL-JSON), stored by the site as aw_study_title; never edited here.
- Headline (post title, H1): at most 70 characters; the topic people search first, the place, and the word "study",
  optionally led by the key finding; plain words, no methodology jargon; only facts in the abstract or summary.
- SEO title (Rank Math): "{Title Case title on the keyphrase} – Animal Writes", at most 60 characters, the en dash
  before the site name the only separator. Focus keyphrase: the headline's topic phrase, word for word in both.
- Slug: a short keyword slug from the headline's words (at most 60 characters).

draft() writes them for a batch of papers with the provider chain (summarise.Summariser backends); check() is the
rule set, so the Research worker (new papers) and the one-off backfill (python -m aw_workers.research_titles
export.json out.json) use the same rules.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import unicodedata

from . import summarise
from .focus_keywords import DANGLING, GENERIC, in_title, phrase_words
from .titles import no_hyphens

log = logging.getLogger("aw_workers.research_titles")

BATCH = int(__import__("os").environ.get("AW_TITLES_BATCH") or 4)
BRAND = " – Animal Writes"
HEADLINE_MAX, SEO_MAX, SLUG_MAX = 70, 60, 60

# Methodology words a reader does not search for (owner: "plain words, no methodology jargon").
JARGON = re.compile(
    r"\b(cross[- ]?sectional|longitudinal|retrospective|prospective|cohort|hedonic|geospatial|spatio[- ]?temporal|"
    r"spatial|temporal|statistical|regression|meta[- ]?analys[ie]s|systematic review|scoping review|descriptive|"
    r"observational|quantitative|qualitative|mixed[- ]methods?|kap|questionnaire[- ]based|hospital[- ]based|"
    r"community[- ]based|paradigm|paradox|perspectives?|insights?|exploring|assessment of|evaluation of|"
    r"seroprevalence|sero[- ]?survey|epidemiolog\w*|phylogenetic|genotyp\w*|molecular|in silico|nexus|landscape of)\b",
    re.I,
)
SLUG_STOP = {"a", "an", "the", "and", "or", "of", "in", "on", "at", "to", "for", "by", "with", "from", "as", "is", "are",
             "was", "were", "be", "its", "their", "than", "that", "into", "how", "what", "why", "who"}

PROMPT = """You write titles for research summaries on an Indian animal-welfare website. Readers are the public, not scientists.
For each paper below write four things:

1. "headline": the page heading, a natural grammatical English sentence or phrase a newspaper would print. At most
   70 characters (aim for 45 to 70). Sentence case (capital first letter and proper nouns only).
   It starts with the topic people in India search for (for example "Dog bites", "Rabies vaccine", "Stray cattle",
   "Human elephant conflict", "Street dogs", "Leopards"), names the place (state, city or district named in the text;
   "India" when no smaller place is named) and says "study". Use one of these shapes:
   - "{Topic} {key finding} in {place}, study finds" e.g. "Dog bite victims in Tamil Nadu often skip rabies doses, study finds"
   - "{Topic} in {place}: ..." is NOT allowed (no colon). Instead: "{Topic} in {place} and what a study found" or
     "{Topic} in {place}, a study of {what was studied}" e.g. "Dog bites in Bidar, Karnataka, a study of injuries and treatment"
   - "{Topic} in {place} {verb} {finding}, study shows" e.g. "Stray dogs in Indian cities linked to lower house prices, study shows"
   Lead with the key finding when the abstract or summary states one clearly; otherwise say what was studied.
   Plain everyday words. Never methodology words such as cross-sectional, retrospective, cohort, hedonic, geospatial,
   spatial, temporal, statistical, systematic review, seroprevalence, epidemiological, molecular, perspectives, paradox,
   exploring, insights. No colon, semicolon, brackets, quotes or hyphens. Use only facts present in the abstract or
   summary given: never add a number, place, animal or finding that is not there.
2. "keyphrase": the headline's topic phrase, 2 to 4 words, lower case, copied word for word from the headline and the
   seo_title (for example "dog bites", "rabies vaccine", "human elephant conflict"). Not a place alone, never "study".
3. "seo_title": 36 to 44 characters, Title Case, a natural phrase built on the keyphrase, with the place and "Study"
   when they fit, joined with ordinary words ("in", "and", "of"), never a list of bare nouns. Examples:
   "Dog Bite Injuries in Bidar Karnataka Study", "Leopards and Striped Hyenas in Jaipur Study",
   "Stray Dogs and House Prices in India Study". No colon, comma, dash, hyphen, pipe or "&" (write "and").
   Never the words "Animal Writes". It is printed with " – Animal Writes" after it.
4. "slug": a short URL slug from the headline's own words: lower case, words joined by "-", at most 60 characters,
   the keyphrase words and the place first, no small words (a, the, of, in, and), no numbers unless essential.

Output only JSON: a list of objects {"id": <id>, "headline": "...", "keyphrase": "...", "seo_title": "...", "slug": "..."},
one per paper, nothing else.

@ITEMS@"""

RETRY = """These answers broke a rule. Write all four fields again for each of these papers, following every rule.
{reasons}"""


def _plain(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _ascii_slug(text: str) -> str:
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
    return re.sub(r"-{2,}", "-", t)


def slug_from(headline: str, keyphrase: str = "") -> str:
    """A slug from the headline's own words when the model's breaks a rule: keyphrase first, small words out, cut whole."""
    words = [w for w in _ascii_slug(headline).split("-") if w and w not in SLUG_STOP]
    lead = [w for w in _ascii_slug(keyphrase).split("-") if w]
    order = lead + [w for w in words if w not in lead]
    out = []
    for w in order:
        if len("-".join(out + [w])) > SLUG_MAX:
            break
        out.append(w)
    return "-".join(out)


def tidy(row: dict) -> dict:
    """The model's answer with the site's title rules applied (no hyphens joining words, the en dash suffix)."""
    h = no_hyphens(_plain(str(row.get("headline", ""))).strip(" .\"'“”"), case=False)
    h = h[:1].upper() + h[1:]
    seo = no_hyphens(re.sub(r"\s*[–—|-]\s*animal writes\s*$", "", _plain(str(row.get("seo_title", ""))), flags=re.I), case=True)
    seo = seo.replace("&", "and").strip(" .")
    kw = no_hyphens(_plain(str(row.get("keyphrase", ""))).lower(), case=False).strip(" .\"'")
    slug = _ascii_slug(str(row.get("slug", "")))
    return {"headline": h, "keyphrase": kw, "seo_title": seo + BRAND if seo else "", "slug": slug}


def _numbers(text: str) -> set[str]:
    return set(re.findall(r"\d+(?:\.\d+)?", text or ""))


def check(t: dict, source_text: str) -> str:
    """'' when the four fields keep every rule, else the first rule broken (sent back to the model)."""
    h, kw, seo, slug = t["headline"], t["keyphrase"], t["seo_title"], t["slug"]
    if not h or len(h) > HEADLINE_MAX:
        return f"headline is {len(h)} characters (at most {HEADLINE_MAX})"
    if not re.search(r"\bstud(y|ies)\b", h, re.I):
        return 'headline does not say "study"'
    if re.search(r"[:;()\[\]\"“”|]", h):
        return "headline has a colon, semicolon, bracket or quote"
    m = JARGON.search(h) or JARGON.search(seo)
    if m:
        return f'uses the methodology word "{m.group(0)}"'
    extra = _numbers(h) - _numbers(source_text)
    if extra:
        return f"headline has numbers not in the text: {', '.join(sorted(extra))}"
    words = kw.split()
    if not 2 <= len(words) <= 4 or words[-1] in DANGLING or words[0] in DANGLING or set(words) <= GENERIC | {"study", "india"}:
        return f'keyphrase "{kw}" is not a 2 to 4 word topic phrase'
    if not in_title(kw, h):
        return f'keyphrase "{kw}" is not word for word in the headline'
    if not seo or len(seo) > SEO_MAX:
        return f"seo_title is {len(seo)} characters with the site name (at most {SEO_MAX})"
    core = seo[: -len(BRAND)]
    if re.search(r"[:,;|&()\[\]–—-]|animal writes", core, re.I):
        return "seo_title has a separator, comma or the site name"
    if not in_title(kw, core):
        return f'keyphrase "{kw}" is not word for word in the seo_title'
    if not slug or len(slug) > SLUG_MAX or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
        return "slug is not lower case words joined by - within 60 characters"
    head = set(phrase_words(no_hyphens(h, case=False))) | {w for w in _ascii_slug(h).split("-")}
    stray = [w for w in slug.split("-") if w not in head]
    if stray:
        return f"slug has words not in the headline: {', '.join(stray)}"
    return ""


def source_text(p: dict) -> str:
    """What a headline's numbers are checked against: the paper's abstract and title (the summary only when there is no
    abstract: a summary can carry a wrong figure, as the Chengalpattu one did, 20 Oct 2026)."""
    return f'{p.get("abstract") or p.get("summary", "")} {p.get("study_title", "")}'


def _lines(papers: list[dict]) -> str:
    return "\n".join(json.dumps({"id": p["id"], "study_title": p["study_title"], "journal": p.get("journal", ""),
                                 "abstract": (p.get("abstract") or "")[:1500], "summary": (p.get("summary") or "")[:1200]},
                                ensure_ascii=False) for p in papers)


def parse(reply: str) -> dict[int, dict]:
    m = re.search(r"\[.*\]", reply or "", re.S)
    if not m:
        return {}
    try:
        rows = json.loads(m.group(0))
    except ValueError:
        return {}
    return {int(r["id"]): r for r in rows if isinstance(r, dict) and str(r.get("id", "")).isdigit()}


def _ask(owner, prompt: str) -> tuple[dict[int, dict], str]:
    for _ in range(2):
        for i, backend in enumerate(owner.backends):
            if not summarise.usable(owner, i):
                continue
            try:
                got = parse(backend.ask(prompt))
            except Exception as exc:  # noqa: BLE001 - next key
                summarise.note_failure(owner, i, exc)
                continue
            if got:
                summarise.note_success(owner, i)
                return got, f"{backend.name}:{backend.model}"
        if not summarise.wait_for_cooldown(owner):
            break
    return {}, ""


def draft(owner, papers: list[dict], tries: int = 3) -> dict[int, dict]:
    """{id: {headline, keyphrase, seo_title, slug, model, problem}} for a batch; a paper whose answer still breaks a
    rule after `tries` calls keeps its last answer with the rule in "problem" (the owner sees it)."""
    out: dict[int, dict] = {}
    todo, reasons = list(papers), {}
    for _ in range(tries):
        if not todo:
            break
        prompt = PROMPT.replace("@ITEMS@", _lines(todo))
        if reasons:
            prompt += "\n\n" + RETRY.format(reasons="\n".join(f"id {i}: {r}" for i, r in reasons.items()))
        got, model = _ask(owner, prompt)
        reasons, again = {}, []
        for p in todo:
            if p["id"] not in got:
                reasons[p["id"]] = "no answer"
                again.append(p)
                continue
            t = tidy(got[p["id"]])
            if t["headline"] and not t["slug"]:
                t["slug"] = slug_from(t["headline"], t["keyphrase"])
            src = source_text(p)
            why = check(t, src)
            if why and why.startswith("slug"):
                t["slug"] = slug_from(t["headline"], t["keyphrase"])
                why = check(t, src)
            out[p["id"]] = dict(t, model=model, problem=why)
            if why:
                reasons[p["id"]] = f'{why} (you wrote {json.dumps({k: t[k] for k in ("headline", "keyphrase", "seo_title", "slug")}, ensure_ascii=False)})'
                again.append(p)
        todo = again
    return out


def main(argv: list[str]) -> int:
    """Backfill drafts: python -m aw_workers.research_titles export.json out.json (keys from AW_KEYS_FILE rows)."""
    import os

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    papers = json.load(open(argv[0]))
    done = json.load(open(argv[1])) if os.path.exists(argv[1]) else {}
    rows = json.load(open(os.environ["AW_KEYS_FILE"])) if os.environ.get("AW_KEYS_FILE") else []
    backends = summarise.backends_from_rows(rows, ledger={}) if rows else summarise.backends_from_env(os.environ, ledger={})
    owner = summarise.Summariser(backends)
    todo = [p for p in papers if str(p["id"]) not in done or done[str(p["id"])].get("problem")]
    log.info("%d papers to draft", len(todo))
    for s in range(0, len(todo), BATCH):
        got = draft(owner, todo[s:s + BATCH])
        done.update({str(k): v for k, v in got.items()})
        json.dump(done, open(argv[1], "w"), ensure_ascii=False, indent=1)
        log.info("%d/%d drafted", min(s + BATCH, len(todo)), len(todo))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
