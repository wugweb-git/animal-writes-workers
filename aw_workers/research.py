"""Research for Resources (owner, 10 Oct 2026: "everything India only").

Papers about animals in India from two open databases (no key): Europe PMC (health and veterinary research) and
Crossref (journal papers). Only papers published on or after the floor (AW_RESEARCH_FLOOR, default 2025-11-01) whose
title or abstract (or, on Europe PMC, an author affiliation) names India or an Indian state, and whose title or abstract
names the topic. Each paper becomes a Resources item of type Research: a short summary written from its abstract, a
link to the paper (DOI), and its own publication date. Sent as backfill, so it publishes with that date.

Run: python -m aw_workers.research   (DRY_RUN=true by default)
"""

from __future__ import annotations

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

from . import ingest, sources, summarise
from .run import truthy
from .sources import Item, Source

log = logging.getLogger("aw_workers.research")
UA = {"User-Agent": "AnimalWritesWorkers/1.0 (https://animalwrites.in; mailto:contact@animalwrites.in)"}

# Topic → words that must appear in the title or abstract (the query itself is broader).
TOPICS = {
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


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


def relevant(title: str, abstract: str, affiliation: str = "") -> bool:
    about_india = bool(INDIA.search(f"{title} {abstract}")) or bool(INDIA.search(affiliation) and INDIA.search(title + " " + abstract[:600]))
    on_topic = any(w in title.lower() for words in TOPICS.values() for w in words)  # the topic in the title itself
    return about_india and on_topic


def europepmc(term: str, floor: date, today: date, get=requests.get) -> list[dict]:
    q = f'(TITLE_ABS:"{term}") AND (TITLE_ABS:"India" OR AFF:"India") AND FIRST_PDATE:[{floor.isoformat()} TO {today.isoformat()}]'
    out, cursor = [], "*"
    for _ in range(5):  # up to 500 per term
        r = get("https://www.ebi.ac.uk/europepmc/webservices/rest/search", headers=UA, timeout=60,
                params={"query": q, "format": "json", "resultType": "core", "pageSize": 100, "cursorMark": cursor})
        r.raise_for_status()
        d = r.json()
        for x in (d.get("resultList") or {}).get("result", []):
            aff = " ".join(a.get("affiliation", "") for a in ((x.get("authorList") or {}).get("author") or []) if isinstance(a, dict))
            doi = x.get("doi") or ""
            out.append({
                "title": clean(x.get("title", "")), "abstract": clean(x.get("abstractText", "")), "affiliation": aff,
                "date": (x.get("firstPublicationDate") or "")[:10],
                "url": f"https://doi.org/{doi}" if doi else f"https://europepmc.org/article/{x.get('source', 'MED')}/{x.get('id')}",
                "journal": ((x.get("journalInfo") or {}).get("journal") or {}).get("title") or x.get("bookOrReportDetails", {}).get("publisher", "") or "Europe PMC",
            })
        nxt = d.get("nextCursorMark")
        if not nxt or nxt == cursor or not (d.get("resultList") or {}).get("result"):
            break
        cursor = nxt
        time.sleep(0.5)
    return out


def crossref(term: str, floor: date, get=requests.get) -> list[dict]:
    r = get("https://api.crossref.org/works", headers=UA, timeout=60, params={
        "query.bibliographic": f"{term} India", "rows": 200, "sort": "relevance",
        "filter": f"from-pub-date:{floor.isoformat()},type:journal-article",
        "select": "DOI,title,container-title,published,issued,abstract,publisher"})
    r.raise_for_status()
    out = []
    for x in r.json()["message"]["items"]:
        parts = ((x.get("published") or x.get("issued") or {}).get("date-parts") or [[None]])[0]
        when = "-".join(f"{p:02d}" if i else str(p) for i, p in enumerate(parts if len(parts) == 3 else [])) if parts and parts[0] else ""
        out.append({"title": clean((x.get("title") or [""])[0]), "abstract": clean(x.get("abstract", "")), "affiliation": "",
                    "date": when, "url": f"https://doi.org/{x['DOI']}",
                    "journal": clean((x.get("container-title") or [""])[0]) or x.get("publisher", "")})
    return out


def collect(floor: date, today: date) -> list[dict]:
    seen, keep = set(), []
    for words in TOPICS.values():
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


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    dry_run = truthy(os.environ.get("DRY_RUN"), default=True)
    today = sources.utc_today()
    days = os.environ.get("AW_RESEARCH_DAYS", "")
    floor = (today - timedelta(days=int(days))) if days.isdigit() else date.fromisoformat(os.environ.get("AW_RESEARCH_FLOOR") or "2025-11-01")
    client = None
    if not dry_run:
        client = ingest.Client(os.environ["WP_BASE_URL"], os.environ["WP_INGEST_USER"], os.environ["WP_INGEST_APP_PASSWORD"])
        try:  # the site's topic list, so each paper gets topics like News does
            raw = client.policy()
            if isinstance(raw.get("config"), dict) and raw["config"].get("sources"):
                sources.load_sources_data(raw["config"])
        except Exception as exc:
            log.warning("policy unavailable: %s", exc)
    backends = summarise.backends_from_env(os.environ, ledger={})
    summariser = summarise.Summariser(backends) if backends and not dry_run else summarise.DryRunSummariser()
    papers = collect(floor, today)
    log.info("research: %d India papers since %s", len(papers), floor)
    src = Source(id="research", name="Research", kind="api", type="research", url="")
    counts, rows = {"sent": 0, "duplicate": 0, "failed": 0}, []
    for p in papers:
        item = Item(source=src, title=p["title"], url=p["url"], date=date.fromisoformat(p["date"]),
                    text=p["abstract"] or p["title"], publisher=p["journal"])
        try:
            summary = summariser.summarise(item)
            topics = summarise.pick_topics(summariser, item, summary, list(sources.TOPICS))
            payload = dict(ingest.build_payload(item, summary, topics), backfill=True)
            result = client.post(payload) if client else "dry-run"
        except ingest.AuthError:
            raise
        except Exception as exc:
            counts["failed"] += 1
            log.error("failed %s: %s", p["url"], exc)
            continue
        counts["duplicate" if result == "duplicate" else "sent"] += 1
        rows.append({"date": p["date"], "journal": p["journal"], "title": p["title"], "url": p["url"], "result": result})
    Path("research-report.json").write_text(json.dumps({"floor": floor.isoformat(), "counts": counts, "items": rows}, ensure_ascii=False, indent=1))
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        lines = [f"### Research (India) since {floor}", "", f"{counts}", "", "| Date | Journal | Title |", "|---|---|---|"]
        lines += [f"| {r['date']} | {r['journal']} | [{r['title'][:90]}]({r['url']}) |" for r in rows]
        Path(summary_file).write_text("\n".join(lines) + "\n")
    print(json.dumps(counts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
