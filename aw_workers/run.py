"""Entry point: python -m aw_workers.run"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from . import documents, enrich, ingest, policy as policy_mod, press, relevance, sources, summarise

log = logging.getLogger("aw_workers")

SEEN_FILE = Path(os.environ.get("AW_SEEN_FILE", ".state/seen.json"))
SPEND_FILE = Path(os.environ.get("AW_SPEND_FILE", ".state/haiku_spend.json"))
TITLES_FILE = Path(os.environ.get("AW_TITLES_FILE", ".state/titles.json"))
TITLE_DAYS = 1  # the same normalised title within a day either side is the same story


@dataclass
class Stats:
    fetched: int = 0
    new: int = 0
    duplicate: int = 0
    skipped: int = 0
    failed: int = 0
    rejected: int = 0  # refused by the relevance gate (logged on the site)
    unavailable: int = 0  # relevance gate could not run: not created, not marked seen, tried again next run
    errors: list[str] = field(default_factory=list)


def truthy(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def load_seen(path: Path) -> set[str]:
    try:
        return set(json.loads(path.read_text()))
    except (OSError, ValueError):
        return set()


def save_seen(path: Path, seen: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(seen)))


def load_titles(path: Path) -> dict[str, list[str]]:
    try:
        data = json.loads(path.read_text())
        return {k: list(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_titles(path: Path, titles: dict[str, list[str]], today) -> None:
    """Keep two weeks of titles: enough for the ±1 day check on late feeds."""
    floor = (today - timedelta(days=14)).isoformat()
    keep = {k: [d for d in v if d >= floor] for k, v in titles.items()}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({k: v for k, v in keep.items() if v}, ensure_ascii=False))


def title_seen(titles: dict[str, list[str]], title: str, when) -> bool:
    key = policy_mod.norm_title(title)
    if not key or when is None:
        return False
    return any(abs((date.fromisoformat(d) - when).days) <= TITLE_DAYS for d in titles.get(key, []))


def remember_title(titles: dict[str, list[str]], title: str, when) -> None:
    key = policy_mod.norm_title(title)
    if key and when is not None and when.isoformat() not in titles.setdefault(key, []):
        titles[key].append(when.isoformat())


def needs_animal_word(item) -> bool:
    """News and Library items found in feeds; documents (own animal filter), AWBI pages and opportunities are exempt."""
    return item.source.type in ("news", "legal") and not item.doc_kind and item.source.kind != "awbi"


def run(
    source_list,
    defaults: dict,
    *,
    dry_run: bool,
    summariser,
    client,
    fetch=sources.fetch,
    today=None,
    seen: set[str] | None = None,
    enricher=None,
    policy=None,
    titles: dict[str, list[str]] | None = None,
    gate=None,
) -> Stats:
    today = today or sources.utc_today()
    seen = seen if seen is not None else set()
    titles = titles if titles is not None else {}
    lookback = int(defaults.get("lookback_days", 14))
    limit = int(defaults.get("max_items_per_source", 15))
    stats = Stats()

    for src in source_list:
        try:
            body = "" if src.kind in sources.SELF_FETCH else fetch(src.url)
            items = sources.collect(src, body, today, lookback, limit)
        except Exception as exc:  # one broken source must not stop the others
            stats.failed += 1
            stats.errors.append(f"{src.id}: fetch/parse failed: {exc}")
            log.error("%s: fetch/parse failed: %s", src.id, exc)
            continue
        stats.fetched += len(items)
        log.info("%s: %d item(s) after filters", src.id, len(items))

        for item in items:
            if item.url in seen:
                stats.duplicate += 1
                continue
            original = item.url
            if enricher and not item.doc_kind:  # documents are PDFs: nothing to resolve or preview
                item.url, item.image = enricher(item.url)
                if item.url != original and item.url in seen:
                    seen.add(original)
                    stats.duplicate += 1
                    continue
            if policy is not None:
                why = policy.verdict(item.url, src.id, f"{item.title} {item.text}")
                if why:
                    seen.add(item.url)
                    stats.skipped += 1
                    log.info("%s: skipped (%s) %s", src.id, why, item.url)
                    continue
            if needs_animal_word(item) and not policy_mod.about_animals(f"{item.title} {item.text}", policy):
                seen.add(item.url)
                stats.skipped += 1
                log.info("%s: skipped (no animal word) %s", src.id, item.url)
                continue
            if item.source.type in ("news", "legal") and title_seen(titles, item.title, item.date):
                seen.add(item.url)
                stats.duplicate += 1
                log.info("%s: duplicate title (same day ±%d) %s", src.id, TITLE_DAYS, item.url)
                continue
            if gate is not None:
                outcome, verdict = gate.admit(relevance.candidate_for(item))
                if outcome == "unavailable":  # not seen: the next run tries again
                    stats.unavailable += 1
                    continue
                if outcome == "reject":
                    seen.add(item.url)
                    if enricher and original != item.url:
                        seen.add(original)
                    stats.rejected += 1
                    continue
            else:
                verdict = None
            try:
                summary = (ingest.opportunity_summary(item) if item.source.type == "opportunity"
                           else summariser.summarise(item))
                topics = (None if item.source.type == "opportunity"
                          else summarise.pick_topics(summariser, item, summary, list(sources.TOPICS)))
                payload = ingest.build_payload(item, summary, topics)
                if verdict is not None:
                    payload["relevance"] = verdict.to_json()
                if dry_run:
                    log.info("DRY_RUN would POST: %s", json.dumps(payload, ensure_ascii=False))
                    remember_title(titles, item.title, item.date)
                    stats.new += 1
                    continue
                result = client.post(payload)
            except ingest.AuthError:
                raise
            except Exception as exc:
                stats.failed += 1
                stats.errors.append(f"{src.id}: {item.url}: {exc}")
                log.error("%s: failed %s: %s", src.id, item.url, exc)
                continue
            seen.add(item.url)
            if enricher and original != item.url:
                seen.add(original)
            if result not in ("blocked", "filtered"):
                remember_title(titles, item.title, item.date)
            if result == "duplicate":
                stats.duplicate += 1
            elif result in ("blocked", "filtered"):
                stats.skipped += 1
            else:
                stats.new += 1
    return stats


def default_enricher(url: str) -> tuple[str, str]:
    """Publisher URL (resolving Google News links) and its preview image."""
    real = enrich.resolve_google_news(url)
    return real, enrich.og_image(real)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    dry_run = truthy(os.environ.get("DRY_RUN"), default=True)

    import yaml
    lists = yaml.safe_load(Path(os.environ.get("AW_SOURCES", "sources.yaml")).read_text(encoding="utf-8")) or {}

    try:
        ledger = json.loads(SPEND_FILE.read_text())
    except (OSError, ValueError):
        ledger = {}
    backends = summarise.backends_from_env(os.environ, ledger=ledger)
    if dry_run and not backends:
        log.warning("DRY_RUN without any provider key: summaries are placeholders")
        summariser = summarise.DryRunSummariser()
    else:
        if not backends:
            log.error("no summary provider key set (ANTHROPIC_API_KEY, GEMINI_API_KEY or GROQ_API_KEY)")
            return 2
        summariser = summarise.Summariser(backends)
        log.info("summary providers, in order: %s", ", ".join(b.name for b in backends))

    client = None
    if not dry_run:
        missing = [k for k in ("WP_BASE_URL", "WP_INGEST_USER", "WP_INGEST_APP_PASSWORD") if not os.environ.get(k)]
        if missing:
            log.error("missing secrets: %s", ", ".join(missing))
            return 2
        client = ingest.Client(
            os.environ["WP_BASE_URL"], os.environ["WP_INGEST_USER"], os.environ["WP_INGEST_APP_PASSWORD"]
        )

    seen = load_seen(SEEN_FILE)
    titles = load_titles(TITLES_FILE)
    log.info("mode: %s", "DRY_RUN" if dry_run else "LIVE")

    # Sourcing policy from WP Admin. Live runs need it reachable; a dry run uses it when credentials exist.
    policy = None
    policy_client = client
    if policy_client is None and all(os.environ.get(k) for k in ("WP_BASE_URL", "WP_INGEST_USER", "WP_INGEST_APP_PASSWORD")):
        policy_client = ingest.Client(os.environ["WP_BASE_URL"], os.environ["WP_INGEST_USER"], os.environ["WP_INGEST_APP_PASSWORD"])
    if policy_client is not None:
        try:
            raw = policy_client.policy()
            policy = policy_mod.Policy.from_json(raw)
            log.info("policy: %d blocked publisher(s), relevance filter %s", len(policy.blocked), "on" if policy.relevance_on else "off")
            # The lists (feeds, Library and opportunity sources, topics, press queries) come from Settings → Sourcing
            # once "Workers use these lists" is on there; otherwise, or if the site is unreachable, from sources.yaml.
            if isinstance(raw.get("config"), dict) and raw["config"].get("sources"):
                lists = raw["config"]
                log.info("lists: from the site (%d sources)", len(lists["sources"]))
            else:
                log.info("lists: from sources.yaml")
        except ingest.AuthError as exc:
            log.error("AUTH FAILURE, stopping: %s", exc)
            print(f"::error::Policy authentication failed: {exc}")
            return 3
        except Exception as exc:  # the site enforces the policy too, so carry on without the pre-filter
            log.warning("policy unavailable, relying on the site to enforce it: %s", exc)
    source_list, defaults = sources.load_sources_data(lists)
    # Opportunities are not fetched (owner, 9 Oct 2026). AW_FETCH_OPPORTUNITIES=1 would turn them back on.
    if not truthy(os.environ.get("AW_FETCH_OPPORTUNITIES"), default=False):
        source_list = [s for s in source_list if s.type != "opportunity"]
    gate = relevance.Gate.from_site(summariser, client or policy_client, dry_run=dry_run)
    try:
        stats = run(source_list, defaults, dry_run=dry_run, summariser=summariser, client=client, seen=seen,
                    enricher=default_enricher, policy=policy, titles=titles, gate=gate)
    except ingest.AuthError as exc:
        log.error("AUTH FAILURE, stopping: %s", exc)
        print(f"::error::Ingest authentication failed: {exc}")
        return 3
    # Press discovery (Media & PR), weekly, through Google News and Google Search.
    try:
        press.run(lists.get("press") or {}, client, dry_run=dry_run, gate=gate)
    except ingest.AuthError:
        raise
    except Exception as exc:
        log.error("press discovery failed: %s", exc)
    if not dry_run:
        save_seen(SEEN_FILE, seen)
        save_titles(TITLES_FILE, titles, sources.utc_today())
        documents.save_state()  # High Court file versions: only remembered when items were really sent
        gate.save()
    if ledger:
        SPEND_FILE.parent.mkdir(parents=True, exist_ok=True)
        SPEND_FILE.write_text(json.dumps(ledger))
        month = max(ledger)
        print(f"haiku spend {month}: ${ledger[month]:.4f} (cap ${float(os.environ.get('HAIKU_MONTHLY_BUDGET_USD') or 4):.2f})")

    print(f"fetched={stats.fetched} new={stats.new} duplicate={stats.duplicate} skipped={stats.skipped} failed={stats.failed} "
          f"rejected={stats.rejected} classifier_unavailable={stats.unavailable}")
    print(f"relevance gate: {gate.counts}")
    if client is not None and not dry_run:
        try:
            client.relevance_alert(stats.unavailable)
        except Exception as exc:  # the alert must never fail the run
            log.warning("relevance alert not sent: %s", exc)
    if summariser.used:
        print("summaries by provider: " + ", ".join(f"{k}={v}" for k, v in sorted(summariser.used.items())))
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a") as fh:
            fh.write(
                f"### {'DRY_RUN' if dry_run else 'LIVE'} run\n\n| fetched | new | duplicate | skipped | rejected | classifier unavailable | failed |\n|---|---|---|---|---|---|---|\n"
                f"| {stats.fetched} | {stats.new} | {stats.duplicate} | {stats.skipped} | {stats.rejected} | {stats.unavailable} | {stats.failed} |\n"
            )
    return 1 if stats.failed and not stats.new and not stats.duplicate and not stats.skipped and not stats.rejected else 0


if __name__ == "__main__":
    sys.exit(main())
