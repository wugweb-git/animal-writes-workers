"""Entry point: python -m aw_workers.run"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import enrich, ingest, policy as policy_mod, sources, summarise

log = logging.getLogger("aw_workers")

SEEN_FILE = Path(os.environ.get("AW_SEEN_FILE", ".state/seen.json"))
SPEND_FILE = Path(os.environ.get("AW_SPEND_FILE", ".state/haiku_spend.json"))


@dataclass
class Stats:
    fetched: int = 0
    new: int = 0
    duplicate: int = 0
    skipped: int = 0
    failed: int = 0
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
) -> Stats:
    today = today or sources.utc_today()
    seen = seen if seen is not None else set()
    lookback = int(defaults.get("lookback_days", 14))
    limit = int(defaults.get("max_items_per_source", 15))
    stats = Stats()

    for src in source_list:
        try:
            items = sources.collect(src, fetch(src.url), today, lookback, limit)
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
            if enricher:
                original = item.url
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
            try:
                summary = summariser.summarise(item)
                payload = ingest.build_payload(item, summary)
                if dry_run:
                    log.info("DRY_RUN would POST: %s", json.dumps(payload, ensure_ascii=False))
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

    source_list, defaults = sources.load_sources(os.environ.get("AW_SOURCES", "sources.yaml"))

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
    log.info("mode: %s", "DRY_RUN" if dry_run else "LIVE")

    # Sourcing policy from WP Admin. Live runs need it reachable; a dry run uses it when credentials exist.
    policy = None
    policy_client = client
    if policy_client is None and all(os.environ.get(k) for k in ("WP_BASE_URL", "WP_INGEST_USER", "WP_INGEST_APP_PASSWORD")):
        policy_client = ingest.Client(os.environ["WP_BASE_URL"], os.environ["WP_INGEST_USER"], os.environ["WP_INGEST_APP_PASSWORD"])
    if policy_client is not None:
        try:
            policy = policy_mod.Policy.from_json(policy_client.policy())
            log.info("policy: %d blocked publisher(s), relevance filter %s", len(policy.blocked), "on" if policy.relevance_on else "off")
        except ingest.AuthError as exc:
            log.error("AUTH FAILURE, stopping: %s", exc)
            print(f"::error::Policy authentication failed: {exc}")
            return 3
        except Exception as exc:  # the site enforces the policy too, so carry on without the pre-filter
            log.warning("policy unavailable, relying on the site to enforce it: %s", exc)
    try:
        stats = run(source_list, defaults, dry_run=dry_run, summariser=summariser, client=client, seen=seen,
                    enricher=default_enricher, policy=policy)
    except ingest.AuthError as exc:
        log.error("AUTH FAILURE, stopping: %s", exc)
        print(f"::error::Ingest authentication failed: {exc}")
        return 3
    if not dry_run:
        save_seen(SEEN_FILE, seen)
    if ledger:
        SPEND_FILE.parent.mkdir(parents=True, exist_ok=True)
        SPEND_FILE.write_text(json.dumps(ledger))
        month = max(ledger)
        print(f"haiku spend {month}: ${ledger[month]:.4f} (cap ${float(os.environ.get('HAIKU_MONTHLY_BUDGET_USD') or 4):.2f})")

    print(f"fetched={stats.fetched} new={stats.new} duplicate={stats.duplicate} skipped={stats.skipped} failed={stats.failed}")
    if summariser.used:
        print("summaries by provider: " + ", ".join(f"{k}={v}" for k, v in sorted(summariser.used.items())))
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a") as fh:
            fh.write(
                f"### {'DRY_RUN' if dry_run else 'LIVE'} run\n\n| fetched | new | duplicate | skipped | failed |\n|---|---|---|---|---|\n"
                f"| {stats.fetched} | {stats.new} | {stats.duplicate} | {stats.skipped} | {stats.failed} |\n"
            )
    return 1 if stats.failed and not stats.new and not stats.duplicate and not stats.skipped else 0


if __name__ == "__main__":
    sys.exit(main())
