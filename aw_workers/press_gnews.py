"""One-off press search through Google News (owner, 10 Oct 2026: no Brave key; "use Google News for this one search").

Coverage of Animal Writes and the 29 November letter campaign from the press floor (2025-11-01) to today, in monthly
windows. Same queries and relevance rule as press discovery; every hit goes to the site as a "Press: to review" draft
(the site refuses duplicates). Nothing is published automatically.

Run: python -m aw_workers.press_gnews   (DRY_RUN=true by default)
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.parse
from datetime import date
from pathlib import Path

import feedparser
import requests
import yaml

from . import enrich, ingest, press
from .backfill import month_windows
from .run import truthy
from .sources import utc_today

log = logging.getLogger("aw_workers.press_gnews")
UA = {"User-Agent": "Mozilla/5.0 (compatible; AnimalWritesWorkers/1.0; +https://animalwrites.in)"}


def search(query: str, after: date, before: date, get=requests.get) -> list[dict]:
    q = f'"{query}"' if " " in query and not query.startswith("#") else query
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": f"{q} after:{after.isoformat()} before:{before.isoformat()}", "hl": "en-IN", "gl": "IN", "ceid": "IN:en"})
    resp = get(url, headers=UA, timeout=30)
    resp.raise_for_status()
    out = []
    for e in feedparser.parse(resp.text).entries:
        when = time.strftime("%Y-%m-%d", e.published_parsed) if getattr(e, "published_parsed", None) else ""
        src = getattr(e, "source", {}) or {}
        out.append({"title": re.sub(r"\s+-\s+[^-]+$", "", e.get("title", "")), "url": e.get("link", ""), "date": when,
                    "publisher": src.get("title", "") if isinstance(src, dict) else ""})
    return out


def collect(client, dry_run: bool, cfg: dict, floor: date, windows) -> tuple[dict, list]:
    """Search each query in each (after, before) window; send every relevant hit as a Press draft."""
    seen, counts, rows = set(), {"new": 0, "duplicate": 0, "failed": 0, "not relevant": 0}, []
    for after, before in windows:
        for query in cfg.get("queries") or []:
            try:
                hits = search(query, after, before)
            except Exception as exc:
                log.warning("%s %s: %s", query, after, exc)
                continue
            time.sleep(1)
            for h in hits:
                if h["date"] and h["date"] < floor.isoformat():
                    continue
                # Google matches loosely: the title itself must name Animal Writes or the letter campaign.
                if not press.RELEVANT.search(h["title"]):
                    counts["not relevant"] += 1
                    continue
                real = press.normalise(enrich.resolve_google_news(h["url"]))
                if real in seen:
                    continue
                seen.add(real)
                payload = {
                    "type": "press", "title": h["title"][:200], "source_url": real,
                    "source_name": h["publisher"] or urllib.parse.urlparse(real).netloc.removeprefix("www."),
                    "source_date": h["date"], "platform": press.platform_of(real),
                    "format": press.format_of(real, press.platform_of(real)), "account_handle": "",
                    "image_url": enrich.og_image(real), "query": query, "feed": "press-google-news",
                }
                if dry_run:
                    log.info("DRY_RUN would POST press: %s", json.dumps(payload, ensure_ascii=False))
                    counts["new"] += 1
                else:
                    try:
                        result = client.post(payload)
                        counts["duplicate" if result == "duplicate" else "new"] += 1
                    except Exception as exc:
                        counts["failed"] += 1
                        log.error("failed %s: %s", real, exc)
                rows.append({"date": h["date"], "publisher": payload["source_name"], "title": payload["title"], "url": real})
    return counts, rows


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    dry_run = truthy(os.environ.get("DRY_RUN"), default=True)
    cfg = (yaml.safe_load(Path("sources.yaml").read_text(encoding="utf-8")) or {}).get("press") or {}
    floor = date.fromisoformat(str(cfg.get("date_floor", "2025-11-01")))
    client = None
    if not dry_run:
        client = ingest.Client(os.environ["WP_BASE_URL"], os.environ["WP_INGEST_USER"], os.environ["WP_INGEST_APP_PASSWORD"])
    counts, rows = collect(client, dry_run, cfg, floor, month_windows(floor, utc_today()))
    Path("press-gnews-report.json").write_text(json.dumps({"counts": counts, "items": rows}, ensure_ascii=False, indent=1))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        lines = [f"### Press via Google News from {floor}", "", f"{counts}", "", "| Date | Publisher | Title |", "|---|---|---|"]
        lines += [f"| {r['date']} | {r['publisher']} | [{r['title']}]({r['url']}) |" for r in sorted(rows, key=lambda r: r["date"], reverse=True)]
        Path(summary).write_text("\n".join(lines) + "\n")
    print(json.dumps(counts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
