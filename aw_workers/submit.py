"""Submit given URLs (owner, 16 Oct 2026: links found by hand for Campaign 2.0 go through the workers, not by hand).

Each line of AW_SUBMIT is "URL | YYYY-MM-DD | Publisher | Title (optional)". The page's own og:title and og:description
are read when it opens; the title in the line is used when it does not (some publishers refuse automated clients).
Every item then goes through the same pipeline as the backfill: animal-word guard, sourcing policy, relevance gate,
summary rules, categories, and is sent as backfill, so it publishes with its own date and items before the floor
(AW_BACKFILL_FLOOR, default 2025-11-01) are refused. Items the workers saw before are sent again; the site refuses
exact duplicates.

Run: AW_SUBMIT="..." python -m aw_workers.submit   (DRY_RUN=true by default)
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
from datetime import date, datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape

import requests

from . import counts, ingest, relevance, sources, summarise, summary_rules
from . import policy as policy_mod
from .backfill import BackfillClient, report
from .run import SEEN_FILE, default_enricher, load_seen, run, save_seen, truthy

log = logging.getLogger("aw_workers.submit")
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36"}


def parse_lines(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        parts = [p.strip() for p in line.split("|")]
        if not parts[0].startswith("http"):
            continue
        out.append({"url": parts[0], "date": date.fromisoformat(parts[1]), "publisher": parts[2] if len(parts) > 2 else "",
                    "title": parts[3] if len(parts) > 3 else ""})
    return out


def _meta(body: str, prop: str) -> str:
    m = re.search(r'<meta[^>]+(?:property|name)=["\']' + prop + r'["\'][^>]+content=["\']([^"\']+)', body, re.I)
    return html.unescape(m.group(1)).strip() if m else ""


def page_meta(url: str, get=requests.get) -> tuple[str, str]:
    """(og:title, og:description) of the page, or ("", "") when it does not open."""
    try:
        r = get(url, headers=UA, timeout=20)
        if r.status_code >= 400:
            return "", ""
        return _meta(r.text, "og:title"), _meta(r.text, "og:description") or _meta(r.text, "description")
    except requests.RequestException:
        return "", ""


def feed_for(row: dict, title: str, text: str) -> str:
    when = format_datetime(datetime(row["date"].year, row["date"].month, row["date"].day, 6, tzinfo=timezone.utc))
    return ('<?xml version="1.0"?><rss version="2.0"><channel><title>submit</title><item>'
            f"<title>{escape(title)}</title><link>{escape(row['url'])}</link><pubDate>{when}</pubDate>"
            f"<description>{escape(text)}</description><source url=\"{escape(row['url'])}\">{escape(row['publisher'])}</source>"
            "</item></channel></rss>")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    dry_run = truthy(os.environ.get("DRY_RUN"), default=True)
    floor = date.fromisoformat(os.environ.get("AW_BACKFILL_FLOOR", "2025-11-01"))
    rows = parse_lines(os.environ.get("AW_SUBMIT", ""))
    if not rows:
        log.error("AW_SUBMIT has no lines")
        return 2
    backends = summarise.backends_from_env(os.environ, ledger={})
    summariser = summarise.Summariser(backends) if backends and not dry_run else summarise.DryRunSummariser()
    if not dry_run and not backends:
        log.error("no summary provider key set")
        return 2
    inner = None
    if all(os.environ.get(k) for k in ("WP_BASE_URL", "WP_INGEST_USER", "WP_INGEST_APP_PASSWORD")):
        inner = ingest.Client(os.environ["WP_BASE_URL"], os.environ["WP_INGEST_USER"], os.environ["WP_INGEST_APP_PASSWORD"])
    elif not dry_run:
        log.error("missing WordPress secrets")
        return 2
    policy = None
    if inner is not None:
        try:
            policy = policy_mod.Policy.from_json(inner.policy())
        except Exception as exc:  # the site applies the policy too
            log.warning("policy unavailable: %s", exc)

    feeds, source_list = {}, []
    for i, row in enumerate(rows):
        title, text = page_meta(row["url"])
        title = title or row["title"]
        if not title:
            log.error("no title for %s: add one to the line", row["url"])
            continue
        pib = "pib.gov.in" in row["url"]  # PIB releases are Library items (official release), as from the pib-releases source
        src = sources.Source(id=f"submit-{i}", name=row["publisher"] or "Submitted", kind="rss",
                             type="legal" if pib else "news", source_type="pib" if pib else "",
                             url=f"submit://{i}", lookback_days=100_000)
        feeds[src.url] = feed_for(row, title, text or title)
        source_list.append(src)

    client = BackfillClient(None if dry_run else inner, floor)
    gate = relevance.Gate.from_site(None if dry_run else summariser, inner, dry_run=dry_run)
    summary_rules.use(inner)
    seen = set()
    stats = run(source_list, {"lookback_days": 100_000, "max_items_per_source": 1}, dry_run=False, summariser=summariser,
                client=client, fetch=lambda url: feeds[url], seen=seen, enricher=default_enricher, policy=policy, gate=gate)
    if not dry_run:
        save_seen(SEEN_FILE, load_seen(SEEN_FILE) | seen)
        gate.save()
    out = report(client.rows, stats, floor)
    counts.write(submitted=len(client.rows), sent=out["sent"], duplicate=len(out["duplicates_exact"]), failed=len(stats.errors))
    for r in client.rows:
        log.info("%s | %s | %s | %s", r["result"], r["date"], r["title"], r["url"])
    print(json.dumps({k: (v if not isinstance(v, list) else len(v)) for k, v in out.items()}, ensure_ascii=False))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write("| Result | Date | Title | URL |\n|---|---|---|---|\n")
            for r in client.rows:
                fh.write(f"| {r['result']} | {r['date']} | {r['title']} | {r['url']} |\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
