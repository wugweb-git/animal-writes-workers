"""One-off backfill (owner, 9 Oct 2026): News and Library items published from 1 Nov 2025 to today.

- Opportunities are not fetched.
- Google News searches run in monthly windows (after:/before:), newest month first; other feeds as far back as they go.
- Same filters as the live runs (Sourcing lists policy on the worker; the site applies its own gate too).
- Never anything dated before the floor (AW_BACKFILL_FLOOR, default 2025-11-01).
- Every item is sent with "backfill": true, so the site saves it as a DRAFT for the owner (no auto-publish, no merge).
- Duplicates are reported, not merged: exact URL duplicates (the site answers "duplicate") and same-story pairs found
  here by title similarity on the same or next day.
- Report: counts per month per section, duplicates, rejected by rule, dead links → backfill-report.json and the
  GitHub Actions step summary.

Run: python -m aw_workers.backfill   (DRY_RUN=true by default, like the live worker)
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.parse
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import requests

from . import counts, ingest, relevance, sources, summarise, summary_rules
from . import policy as policy_mod
from .run import SEEN_FILE, default_enricher, load_seen, run, save_seen, truthy

log = logging.getLogger("aw_workers.backfill")
REPORT = Path("backfill-report.json")


def month_windows(floor: date, today: date) -> list[tuple[date, date]]:
    """[(first day, first day of next month)] from today's month back to the floor's month, newest first."""
    out, start = [], date(today.year, today.month, 1)
    while start >= date(floor.year, floor.month, 1):
        nxt = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
        out.append((max(start, floor), min(nxt, today + timedelta(days=1))))
        start = date(start.year - (start.month == 1), (start.month - 2) % 12 + 1, 1)
    return out


def windowed(src: sources.Source, after: date, before: date) -> sources.Source:
    """The same Google News search, limited to one window."""
    parts = urllib.parse.urlsplit(src.url)
    qs = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
    q = qs.get("q", [""])[0]
    qs["q"] = [f"{q} after:{after.isoformat()} before:{before.isoformat()}"]
    url = urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(qs, doseq=True)))
    return replace(src, id=f"{src.id}@{after:%Y-%m}", url=url)


def plan(source_list: list[sources.Source], floor: date, today: date) -> list[sources.Source]:
    days = (today - floor).days
    google = [s for s in source_list if s.type != "opportunity" and "news.google.com/rss/search" in s.url]
    other = [replace(s, lookback_days=days) for s in source_list if s.type != "opportunity" and s not in google]
    out = list(other)
    for after, before in month_windows(floor, today):  # newest month first
        out += [replace(windowed(s, after, before), lookback_days=days) for s in google]
    return out


def title_words(t: str) -> set[str]:
    return {w for w in re.findall(r"[a-zऀ-ॿ]{4,}", t.lower())}


def same_story_pairs(rows: list[dict]) -> list[dict]:
    """Pairs with ≥ 0.6 title-word overlap, published the same or next day (reported, never merged)."""
    pairs = []
    for i, a in enumerate(rows):
        wa = title_words(a["title"])
        for b in rows[i + 1:]:
            if a["section"] != b["section"] or abs((date.fromisoformat(a["date"]) - date.fromisoformat(b["date"])).days) > 1:
                continue
            wb = title_words(b["title"])
            if wa and wb and len(wa & wb) / len(wa | wb) >= 0.6:
                pairs.append({"kept": a["url"], "also": b["url"], "title": a["title"], "why": "similar title, same or next day"})
    return pairs


class BackfillClient:
    """Wraps the ingest client: marks every item as backfill, checks the link, and records the outcome."""

    def __init__(self, client, floor: date):
        self.client, self.floor, self.rows = client, floor, []

    def policy(self):
        return self.client.policy() if self.client else {}

    def story(self, story_id: int) -> dict:
        return self.client.story(story_id)

    def same_story(self, body: dict) -> dict:
        return self.client.same_story(body)

    def post(self, payload: dict) -> str:
        day = (payload.get("source_date") or "")[:10]
        if day and day < self.floor.isoformat():
            self.rows.append(self._row(payload, "before floor"))
            return "filtered"
        payload = dict(payload, backfill=True)
        dead = False
        try:
            r = requests.head(payload["source_url"], allow_redirects=True, timeout=12, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 405:
                r = requests.get(payload["source_url"], allow_redirects=True, timeout=12, stream=True, headers={"User-Agent": "Mozilla/5.0"})
            dead = r.status_code >= 400
        except requests.RequestException:
            dead = True
        result = self.client.post(payload) if self.client else "dry-run"
        self.rows.append(self._row(payload, result, dead))
        return result

    @staticmethod
    def _row(payload: dict, result: str, dead: bool = False) -> dict:
        return {"section": "Library" if payload.get("type") == "legal" else "News", "date": (payload.get("source_date") or "")[:10] or date.today().isoformat(),
                "title": payload.get("title", ""), "url": payload.get("source_url", ""), "result": result, "dead": dead}


def report(rows: list[dict], stats, floor: date) -> dict:
    per_month = defaultdict(Counter)
    for r in rows:
        if r["result"] in ("created", "pending", "draft", "dry-run", "new", "published", "ok"):
            per_month[r["date"][:7]][r["section"]] += 1
    posted = [r for r in rows if r["result"] not in ("duplicate", "filtered", "blocked", "before floor")]
    out = {
        "floor": floor.isoformat(),
        "per_month": {m: dict(c) for m, c in sorted(per_month.items(), reverse=True)},
        "sent": len(posted),
        "duplicates_exact": [r["url"] for r in rows if r["result"] == "duplicate"],
        "duplicates_same_story": same_story_pairs(sorted(posted, key=lambda r: r["date"], reverse=True)),
        "rejected_by_rule": {"worker_policy": stats.skipped, "relevance_gate": stats.rejected, "classifier_unavailable": stats.unavailable,
                             "site": sum(1 for r in rows if r["result"] in ("filtered", "blocked"))},
        "dead_links": [r["url"] for r in rows if r["dead"]],
        "failed": stats.errors[:50],
    }
    REPORT.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        lines = ["### Backfill from " + out["floor"], "", "| Month | News | Library |", "|---|---|---|"]
        lines += [f"| {m} | {c.get('News', 0)} | {c.get('Library', 0)} |" for m, c in out["per_month"].items()]
        lines += ["", f"Sent as drafts: {out['sent']} · exact duplicates: {len(out['duplicates_exact'])} · same-story pairs: {len(out['duplicates_same_story'])} · "
                  f"rejected by rule: {out['rejected_by_rule']} · dead links: {len(out['dead_links'])} · failed: {len(stats.errors)}"]
        Path(summary).write_text("\n".join(lines) + "\n")
    return out


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    dry_run = truthy(os.environ.get("DRY_RUN"), default=True)
    floor = date.fromisoformat(os.environ.get("AW_BACKFILL_FLOOR", "2025-11-01"))
    today = sources.utc_today()
    os.environ.setdefault("AW_SC_YEARS", ",".join(str(y) for y in range(floor.year, today.year + 1)))

    import yaml
    lists = yaml.safe_load(Path(os.environ.get("AW_SOURCES", "sources.yaml")).read_text(encoding="utf-8")) or {}
    backends = summarise.backends_from_env(os.environ, ledger={})
    summariser = summarise.Summariser(backends) if backends and not dry_run else summarise.DryRunSummariser()  # dry runs spend nothing
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
            raw = inner.policy()
            policy = policy_mod.Policy.from_json(raw)
            if isinstance(raw.get("config"), dict) and raw["config"].get("sources"):
                lists = sources.with_v2_blocks(raw["config"], raw)
        except Exception as exc:  # the site applies the policy too
            log.warning("policy unavailable: %s", exc)
    source_list, defaults = sources.load_sources_data(lists, relevance=raw.get("relevance") if policy is not None else None)
    defaults = dict(defaults, max_items_per_source=int(os.environ.get("AW_BACKFILL_PER_SOURCE", "100")))
    plan_list = plan(source_list, floor, today)
    log.info("backfill %s → %s: %d source runs (opportunities skipped)", floor, today, len(plan_list))

    # Newest first inside each source too.
    original = sources.collect
    sources.collect = lambda src, body, t, lb, limit: sorted(original(src, body, t, lb, 10_000), key=lambda i: i.date or t, reverse=True)[:limit]
    client = BackfillClient(None if dry_run else inner, floor)
    gate = relevance.Gate.from_site(None if dry_run else summariser, inner, dry_run=dry_run)
    summary_rules.use(inner)
    seen = load_seen(SEEN_FILE)
    try:
        stats = run(plan_list, defaults, dry_run=False, summariser=summariser, client=client, seen=seen,
                    enricher=default_enricher, policy=policy, today=today, gate=gate)
    finally:
        sources.collect = original
    if not dry_run:
        save_seen(SEEN_FILE, seen)
        gate.save()
    log.info("relevance gate: %s (rejected %d, classifier unavailable %d)", gate.counts, stats.rejected, stats.unavailable)
    if inner is not None and not dry_run:
        try:
            inner.relevance_alert(stats.unavailable)
        except Exception as exc:
            log.warning("relevance alert not sent: %s", exc)
    out = report(client.rows, stats, floor)
    print(json.dumps({k: (v if not isinstance(v, list) else len(v)) for k, v in out.items()}, ensure_ascii=False))
    counts.write(sent=out["sent"], duplicate=len(out["duplicates_exact"]), dead_links=len(out["dead_links"]), failed=len(stats.errors))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
