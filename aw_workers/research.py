"""Research for Resources (owner, 10 Oct 2026: "everything India only").

Papers about animals in India from two open databases (no key): Europe PMC (health and veterinary research) and
Crossref (journal papers). Only papers published on or after the floor (AW_RESEARCH_FLOOR, default 2025-11-01) whose
title or abstract (or, on Europe PMC, an author affiliation) names India or an Indian state, and whose title or abstract
names the subject. Each paper becomes a Resources item of type Research: a short summary written from its abstract, a
link to the paper (DOI), and its own publication date. Sent as backfill, so it publishes with that date.

Run: python -m aw_workers.research   (DRY_RUN=true by default)
"""

from __future__ import annotations

import difflib
import html
import json
import logging
import os
import re
import time
import urllib.parse
from datetime import date, timedelta
from pathlib import Path

import requests

from . import counts as run_counts, ingest, relevance, research_titles, sources, summarise, summary_rules
from .run import truthy
from .sources import Item, Source

log = logging.getLogger("aw_workers.research")
UA = {"User-Agent": "AnimalWritesWorkers/1.0 (https://animalwrites.in; mailto:contact@animalwrites.in)"}

# Subject → words that must appear in the title or abstract (the query itself is broader).
SUBJECTS = {
    "community dogs": ["stray dog", "street dog", "free-roaming dog", "free roaming dog", "community dog", "dog population"],
    "rabies and dog bites": ["rabies", "dog bite", "animal bite"],
    "animal birth control": ["animal birth control", "sterilisation", "sterilization", "neuter", "abc programme", "abc program"],
    "cattle and gaushalas": ["gaushala", "goshala", "cow shelter", "stray cattle", "cattle welfare", "dairy cattle welfare", "bovine welfare"],
    "cruelty and welfare": ["animal welfare", "animal cruelty", "cruelty to animals", "animal rights", "prevention of cruelty"],
    "human–wildlife conflict": ["human-wildlife conflict", "human–wildlife conflict", "human-elephant", "human–elephant", "leopard", "macaque", "monkey menace"],
}
INDIA = re.compile(
    r"\bindia\b|\bindian\b|andhra|arunachal|assam|bihar|chhattisgarh|goa\b|gujarat|haryana|himachal|jharkhand|karnataka|"
    r"kerala|madhya pradesh|maharashtra|manipur|meghalaya|mizoram|nagaland|odisha|orissa|punjab|rajasthan|sikkim|tamil nadu|"
    r"telangana|tripura|uttar pradesh|uttarakhand|west bengal|delhi|jammu|kashmir|ladakh|puducherry|chandigarh|mumbai|"
    r"kolkata|chennai|bengaluru|bangalore|hyderabad", re.I)


# Human medicine that shares the word "sterilisation" (SEO audit 10 Oct 2026): family planning, dental, eye and
# instrument sterilisation papers are not about animals.
HUMAN_MED = re.compile(
    r"\b(female|male|surgical|tubal|women'?s?) sterili[sz]ation|tubectomy|vasectomy|family planning|contracepti\w*|"
    r"dental|teeth|tooth|root canal|endodont\w*|pulpotomy|ophthalm\w*|endophthalmitis|cataract|\beye\b|autoclav\w*|"
    r"steam sterili[sz]\w*|surgical instruments?|instrument packing|patient files|central sterile|forced sterili[sz]ation|"
    r"reproductive (?:health|chronicity)|nfhs",
    re.I,
)
ANIMAL = re.compile(r"\b(animals?|dogs?|canine|cats?|feline|stray|street|free[- ]roaming|pets?|cattle|wildlife|veterinar\w*|livestock|"
                    r"macaques?|monkeys?|leopards?|elephants?|rabies)\b", re.I)
# A paper set in another country (Bali, Morocco, Sumatra...) that only mentions India in passing is not India research.
ELSEWHERE = re.compile(
    r"\b(bali|indonesia\w*|sumatra\w*|java|morocc\w*|nepal\w*|bangladesh\w*|pakistan\w*|sri lanka\w*|bhutan\w*|"
    r"myanmar|china|chinese|africa\w*|kenya\w*|tanzania\w*|ethiopia\w*|nigeria\w*|brazil\w*|philippines|vietnam\w*|"
    r"thailand|malaysia\w*|cambodia\w*|europe\w*|america\w*|usa|mexic\w*|peru|chile)\b",
    re.I,
)


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


def relevant(title: str, abstract: str, affiliation: str = "") -> bool:
    """India research about animals. An author affiliation never counts: India must be named in the title, or in
    the abstract when the title names no other country. Human-medicine sterilisation papers are out, and a paper
    that matched only through "sterilisation" must name an animal in its title."""
    if INDIA.search(title):
        about_india = True
    else:
        about_india = bool(INDIA.search(abstract)) and not ELSEWHERE.search(title)
    low = title.lower()
    hits = [w for words in SUBJECTS.values() for w in words if w in low]  # the subject in the title itself
    if not about_india or not hits:
        return False
    if HUMAN_MED.search(title) or HUMAN_MED.search(abstract[:600]) and not ANIMAL.search(title):
        return False
    if all(w in ("sterilisation", "sterilization", "neuter") for w in hits) and not ANIMAL.search(title):
        return False
    return True


def europepmc(term: str, floor: date, today: date, get=requests.get) -> list[dict]:
    q = f'(TITLE_ABS:"{term}") AND (TITLE_ABS:"India" OR AFF:"India") AND FIRST_PDATE:[{floor.isoformat()} TO {today.isoformat()}]'
    out, cursor = [], "*"
    for _ in range(5):  # up to 500 per term
        r = get("https://www.ebi.ac.uk/europepmc/webservices/rest/search", headers=UA, timeout=60,
                params={"query": q, "format": "json", "resultType": "core", "pageSize": 100, "cursorMark": cursor})
        r.raise_for_status()
        d = r.json()
        for x in (d.get("resultList") or {}).get("result", []):
            people = [a for a in ((x.get("authorList") or {}).get("author") or []) if isinstance(a, dict)]
            aff = " ".join(a.get("affiliation", "") for a in people)
            doi = x.get("doi") or ""
            out.append({
                "title": clean(x.get("title", "")), "abstract": clean(x.get("abstractText", "")), "affiliation": aff,
                "date": (x.get("firstPublicationDate") or "")[:10],
                "url": f"https://doi.org/{doi}" if doi else f"https://europepmc.org/article/{x.get('source', 'MED')}/{x.get('id')}",
                "journal": ((x.get("journalInfo") or {}).get("journal") or {}).get("title") or x.get("bookOrReportDetails", {}).get("publisher", "") or "Europe PMC",
                "authors": authors_of(people),
            })
        nxt = d.get("nextCursorMark")
        if not nxt or nxt == cursor or not (d.get("resultList") or {}).get("result"):
            break
        cursor = nxt
        time.sleep(0.5)
    return out


def authors_of(people: list) -> list[str]:
    """Author names as printed ("Given Family"), from Europe PMC (fullName / firstName + lastName) or Crossref/CSL
    (given + family, or a group "name"). The site shows them in the item's Sources block (owner, 20 Oct 2026)."""
    out = []
    for a in people or []:
        if not isinstance(a, dict):
            continue
        name = " ".join(x for x in (a.get("given") or a.get("firstName") or "", a.get("family") or a.get("lastName") or "") if x).strip()
        name = clean(name or a.get("name") or a.get("fullName") or a.get("collectiveName") or "")
        if name and name not in out:
            out.append(name)
    return out


def crossref(term: str, floor: date, get=requests.get) -> list[dict]:
    r = get("https://api.crossref.org/works", headers=UA, timeout=60, params={
        "query.bibliographic": f"{term} India", "rows": 200, "sort": "relevance",
        "filter": f"from-pub-date:{floor.isoformat()},type:journal-article",
        "select": "DOI,title,container-title,published,issued,abstract,publisher,author"})
    r.raise_for_status()
    out = []
    for x in r.json()["message"]["items"]:
        parts = ((x.get("published") or x.get("issued") or {}).get("date-parts") or [[None]])[0]
        when = "-".join(f"{p:02d}" if i else str(p) for i, p in enumerate(parts if len(parts) == 3 else [])) if parts and parts[0] else ""
        out.append({"title": clean((x.get("title") or [""])[0]), "abstract": clean(x.get("abstract", "")), "affiliation": "",
                    "date": when, "url": f"https://doi.org/{x['DOI']}",
                    "journal": clean((x.get("container-title") or [""])[0]) or x.get("publisher", ""),
                    "authors": authors_of(x.get("author") or [])})
    return out


KNOWN_FILE = Path(os.environ.get("AW_RESEARCH_KNOWN", ".state/research-known.json"))
EXISTS_BATCH = 500
_DOI = re.compile(r"^https?://(?:dx\.)?doi\.org/(10\.[^\s?#]+)", re.I)


def doi_of(url: str) -> str:
    """The DOI of a doi.org link, lower case ('' otherwise)."""
    m = _DOI.match(url or "")
    return m.group(1).lower().rstrip("/") if m else ""


def load_known(path: Path = KNOWN_FILE) -> set[str]:
    try:
        return set(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return set()


def save_known(known: set[str], path: Path = KNOWN_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(known)), encoding="utf-8")


SAME_STUDY = 0.9


def norm_title(t: str) -> str:
    """Lower case, letters and digits only, single spaces (an en dash and a hyphen read the same)."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", html.unescape(t or "").lower()).split())


_HONORIFIC = {"mr", "mrs", "ms", "miss", "dr", "prof", "professor", "sri", "smt", "shri"}


def author_key(name: str) -> str:
    """First author as "surname initial", lower case: "Nitin Sekar" and "N. Sekar" and "Sekar, Nitin" are "sekar n".
    Honorifics are dropped, and of a list packed into one string ("Ms. A B, Ms. C D") only the first name counts."""
    name = (name or "").strip()
    if "," in name:
        head, _, rest = name.partition(",")
        rest_words = [w for w in re.findall(r"[a-z]+", rest.lower()) if w not in _HONORIFIC]
        name = f"{rest.strip()} {head.strip()}" if "," not in rest and 0 < len(rest_words) <= 2 else head  # Family, Given
    words = [w for w in re.findall(r"[a-z]+", name.lower()) if w not in _HONORIFIC]
    return f"{words[-1]} {words[0][0]}" if len(words) >= 2 else (words[0] if words else "")


def same_study(p: dict, studies: list[dict]) -> tuple[str, dict]:
    """The site item this paper may be under another DOI or URL (preprint and journal version; owner, 24 Oct 2026):
    - ("duplicate", item): normalised titles alike at >= 0.9 and, both listing authors, the same first author
      (surname and initial). Skipped before any AI step (20054 was 12573 again).
    - ("possible", item): titles alike at >= 0.9 and the same year, but one of the two lists no authors. Not skipped:
      sent held as a possible duplicate, with a link to the item, for a person to decide.
    - ("", {}) otherwise. Alike titles with different first authors are different papers."""
    title = norm_title(p.get("title", ""))
    if not title:
        return "", {}
    first = author_key((p.get("authors") or [""])[0])
    year = str(p.get("date", ""))[:4]
    possible: dict = {}
    for s in studies:
        if difflib.SequenceMatcher(None, title, norm_title(s.get("title", ""))).ratio() < SAME_STUDY:
            continue
        theirs = author_key(s.get("first_author", ""))
        if first and theirs:
            if first == theirs:
                return "duplicate", s
            continue
        if not possible and year and year == str(s.get("year", "")):
            possible = s
    return ("possible", possible) if possible else ("", {})


def possible_duplicate_hold(s: dict) -> str:
    link = s.get("url") or (f"post {s.get('id')}" if s.get("id") else "")
    return f"possible duplicate of \"{s.get('title', '')}\" ({link}): same title and year, authors missing on one side"


def drop_existing(papers: list[dict], client, known: set[str]) -> tuple[list[dict], int]:
    """Papers the site does not have yet, before any AI step (owner, 24 Oct 2026: a hand-started run summarised a year of
    papers, most already on the site). Known DOIs from earlier runs first, then one aw/v1/exists call per 500 papers;
    DOIs found on the site are added to `known`. If the site cannot answer, the papers are kept (the ingest still
    refuses duplicates). Returns (papers to process, how many are already on the site)."""
    fresh = [p for p in papers if not (doi_of(p["url"]) and doi_of(p["url"]) in known)]
    already = len(papers) - len(fresh)
    if client is None or not fresh:
        return fresh, already
    keep: list[dict] = []
    studies: list[dict] = []
    for i in range(0, len(fresh), EXISTS_BATCH):
        chunk = fresh[i:i + EXISTS_BATCH]
        dois = [d for d in (doi_of(p["url"]) for p in chunk) if d]
        try:
            found = client.exists(dois, [p["url"] for p in chunk], index=(i == 0))
        except ingest.AuthError:
            raise
        except Exception as exc:  # endpoint missing or the site busy: keep them, the ingest refuses duplicates anyway
            log.warning("research: could not ask the site which papers it has (%s); checking them one by one", exc)
            keep += fresh[i:]
            break
        if i == 0:
            studies = found.get("studies") or []
        have_dois = {str(d).lower() for d in found.get("dois") or []}
        have_urls = {str(u) for u in found.get("urls") or []}
        known |= have_dois
        for p in chunk:
            if doi_of(p["url"]) in have_dois or p["url"] in have_urls:
                already += 1
            else:
                kind, twin = same_study(p, studies)
                if kind == "duplicate":
                    already += 1
                    log.info("research: %s is the same study as site item %s; skipped", p["url"], twin.get("id"))
                    if doi_of(p["url"]):
                        known.add(doi_of(p["url"]))
                    continue
                if kind == "possible":
                    p["possible_duplicate"] = twin  # sent held for a person (main)
                keep.append(p)
    return keep, already


def collect(floor: date, today: date) -> list[dict]:
    seen, keep = set(), []
    for words in SUBJECTS.values():
        for term in words[:3]:
            for fetch in (lambda t=term: europepmc(t, floor, today), lambda t=term: crossref(t, floor)):
                try:
                    found = fetch()
                except Exception as exc:  # one database failing must not stop the other
                    log.warning("%s: %s", term, exc)
                    continue
                for p in found:
                    key = p["url"].lower()
                    if key in seen or not p["title"] or not p["date"] or p["date"] < floor.isoformat():
                        continue
                    if not relevant(p["title"], p["abstract"], p["affiliation"]):
                        continue
                    seen.add(key)
                    keep.append(p)
                time.sleep(1)
    keep.sort(key=lambda p: p["date"], reverse=True)
    return keep


def india_doubt(p: dict, verdict=None) -> str:
    """'' when the paper is India research, else why a person must look (owner, 20 Oct 2026: a list of foreign place
    names always misses some, so India must be confirmed, not just a foreign name absent). Confirmed: India or an
    Indian state, district or city named in the title; or named in the abstract and the AI check says the study itself
    was done in India."""
    if INDIA.search(p.get("title", "")):
        return ""
    if INDIA.search(p.get("abstract", "")) and verdict is not None and verdict.india:
        return ""
    if not INDIA.search(p.get("abstract", "")):
        return "India research not confirmed: India is not named in the title or abstract"
    return "India research not confirmed: the AI check says the study was not done in India"


def headline_fields(summariser, p: dict, summary: str) -> dict:
    """The item's headline (post title), SEO title, focus keyphrase and slug (research_titles.py, owner 20 Oct 2026);
    the paper's own title goes as study_title. {} when no answer keeps every rule: the site then uses the paper's title."""
    if not getattr(summariser, "backends", None):
        return {}
    paper = {"id": 1, "study_title": p["title"], "journal": p.get("journal", ""), "abstract": p.get("abstract", ""), "summary": summary}
    t = research_titles.draft(summariser, [paper]).get(1)
    if not t or t.get("problem"):
        log.warning("no headline for %s: %s", p["url"], (t or {}).get("problem", "no answer"))
        return {}
    return {"title": t["headline"], "seo_title": t["seo_title"], "focus_keyword": t["keyphrase"], "slug": t["slug"]}


NO_HEADLINE = "no headline (the paper's own title would be published as the headline)"


def with_headline(payload: dict, fields: dict) -> dict:
    """The three-field model (owner, 24 Oct 2026): study title in Sources, a headline of at most 70 characters, an SEO
    title. Without a headline that keeps every rule the item is held for a person, never published under the paper's
    raw title (20054 and 20139 went out that way)."""
    payload.update(fields)
    if not (fields.get("title") and fields.get("slug") and fields.get("seo_title")):
        payload["hold"] = "; ".join(filter(None, [payload.get("hold", ""), NO_HEADLINE]))
    return payload


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    dry_run = truthy(os.environ.get("DRY_RUN"), default=True)
    today = sources.utc_today()
    days = os.environ.get("AW_RESEARCH_DAYS", "")
    floor = (today - timedelta(days=int(days))) if days.isdigit() else date.fromisoformat(os.environ.get("AW_RESEARCH_FLOOR") or "2025-11-01")
    client = None
    if not dry_run:
        client = ingest.Client(os.environ["WP_BASE_URL"], os.environ["WP_INGEST_USER"], os.environ["WP_INGEST_APP_PASSWORD"])
        try:  # the site's category list, so each paper gets categories like News does
            raw = client.policy()
            site = sources.site_lists(raw)
            if site is not None:
                sources.load_sources_data(site, relevance=raw.get("relevance"))
        except Exception as exc:
            log.warning("policy unavailable: %s", exc)
    backends = summarise.backends_from_env(os.environ, ledger={})
    summariser = summarise.Summariser(backends) if backends and not dry_run else summarise.DryRunSummariser()
    gate = relevance.Gate.from_site(None if dry_run else summariser, client, dry_run=dry_run)
    summary_rules.use(client)
    papers = collect(floor, today)
    known = load_known()
    todo, already = drop_existing(papers, client, known)
    log.info("research: %d India papers since %s; %d already on the site; %d to check", len(papers), floor, already, len(todo))
    src = Source(id="research", name="Research", kind="api", type="research", url="")
    counts, rows = {"found": len(papers), "already on site": already, "summarised": 0, "sent": 0, "duplicate": 0, "failed": 0,
                    "rejected": 0, "classifier unavailable": 0}, []
    for p in todo:
        item = Item(source=src, title=p["title"], url=p["url"], date=date.fromisoformat(p["date"]),
                    text=p["abstract"] or p["title"], publisher=p["journal"])
        outcome, verdict = gate.admit(relevance.Candidate(kind="research", title=item.title, url=item.url, text=item.text,
                                                          source=item.publisher, issuer=item.publisher, feed="research"))
        doubt = india_doubt(p, verdict)
        if outcome != "pass" and not (outcome == "reject" and verdict is not None and verdict.relevant and doubt):
            counts["rejected" if outcome == "reject" else "classifier unavailable"] += 1
            continue
        try:
            summary = summariser.summarise(item)
            counts["summarised"] += 1
            ai_cat = gate.category_slug(verdict.category) if verdict is not None and hasattr(gate, "category_slug") else ""
            cats = summarise.pick_categories(summariser, item, summary, sources.allowed_categories("research"),
                                             ai_choice=ai_cat)
            payload = dict(ingest.build_payload(item, summary, cats), backfill=True, study_title=p["title"],
                           authors=p.get("authors") or [], journal=p.get("journal") or "")  # the study's credit (Sources block)
            with_headline(payload, headline_fields(summariser, p, summary))
            if p.get("possible_duplicate"):  # same title and year, authors missing on one side (owner, 24 Oct 2026)
                payload["hold"] = "; ".join(filter(None, [payload.get("hold", ""), possible_duplicate_hold(p["possible_duplicate"])]))
            if doubt:  # India not confirmed: sent for review, never published by itself (owner, 20 Oct 2026)
                payload["hold"] = "; ".join(filter(None, [payload.get("hold", ""), doubt]))
            if getattr(summariser, "last_numbers", None):  # a number not in the source: held for review (owner, 20 Oct 2026)
                payload["hold"] = "; ".join(filter(None, [payload.get("hold", ""), *summariser.last_numbers]))
            if getattr(summariser, "last_short", False):
                payload["summary_short"] = True  # still below the minimum after the fallback: published, marked
            if verdict is not None:
                payload["relevance"] = verdict.to_json()
            result = client.post(payload) if client else "dry-run"
        except ingest.AuthError:
            raise
        except Exception as exc:
            counts["failed"] += 1
            log.error("failed %s: %s", p["url"], exc)
            continue
        counts["duplicate" if result == "duplicate" else "sent"] += 1
        if client and doi_of(p["url"]):
            known.add(doi_of(p["url"]))  # on the site now (sent, held or a duplicate): never summarised again
        rows.append({"date": p["date"], "journal": p["journal"], "title": p["title"], "url": p["url"], "result": result})
    if not dry_run:
        gate.save()
        save_known(known)
    Path("research-report.json").write_text(json.dumps({"floor": floor.isoformat(), "counts": counts, "items": rows}, ensure_ascii=False, indent=1))
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        lines = [f"### Research (India) since {floor}", "", f"{counts}", "", "| Date | Journal | Title |", "|---|---|---|"]
        lines += [f"| {r['date']} | {r['journal']} | [{r['title'][:90]}]({r['url']}) |" for r in rows]
        Path(summary_file).write_text("\n".join(lines) + "\n")
    print(json.dumps(counts))
    run_counts.write(**{k.replace(" ", "_"): v for k, v in counts.items()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
