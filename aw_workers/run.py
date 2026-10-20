"""Entry point: python -m aw_workers.run"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from . import documents, enrich, ingest, merge, policy as policy_mod, press, relay, relevance, sources, summarise, summary_rules

# How far back Google News searches look (AW_WINDOW, e.g. "4d" to catch up after an outage).
WINDOW = os.environ.get("AW_WINDOW") or "2d"
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
    published: int = 0  # created and published by the site's auto-review (the rest wait in To review)
    sources: int = 0  # sources tried
    sources_failed: int = 0  # sources that could not be fetched, even through the fallback
    stopped_early: bool = False  # time budget reached; the rest is picked up by the next run
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
    checkpoint=None,
    deadline: float | None = None,
) -> Stats:
    """`checkpoint()` is called after every item that changed the state (live runs save seen/titles/verdicts there, so
    a run cut short keeps its progress). `deadline` (time.monotonic()) stops the run cleanly before the job timeout."""
    today = today or sources.utc_today()
    seen = seen if seen is not None else set()
    titles = titles if titles is not None else {}
    lookback = int(defaults.get("lookback_days", 14))
    limit = int(defaults.get("max_items_per_source", 15))
    stats = Stats()

    def save():
        if checkpoint is not None:
            checkpoint()

    for src in source_list:
        if deadline is not None and time.monotonic() > deadline:
            stats.stopped_early = True
            log.warning("time budget reached: stopping before %s; the next run carries on", src.id)
            break
        stats.sources += 1
        try:
            body = "" if src.kind in sources.SELF_FETCH else fetch(src.url)
            used = src
        except Exception as exc:
            # A failed Google News search is tried on Bing News; a publisher feed that refuses GitHub's servers, through
            # a Google News search of its site.
            alt = sources.bing_fallback(src) or sources.site_search_fallback(src)
            if alt is None:
                body, used = None, src
                err = exc
            else:
                via = "Bing News" if "bing.com" in alt.url else "Google News (site search)"
                log.warning("%s: %s; trying %s instead", src.id, exc, via)
                try:
                    body, used = fetch(alt.url), alt
                except Exception as exc2:
                    body, used, err = None, src, f"{exc} / {via}: {exc2}"
            if body is None:  # one broken source must not stop the others
                stats.failed += 1
                stats.sources_failed += 1
                stats.errors.append(f"{src.id}: fetch/parse failed: {err}")
                log.error("%s: fetch/parse failed: %s", src.id, err)
                continue
        try:
            # Everything fresh, newest first; the cap applies to items not seen yet, so old seen stories at the top of a
            # feed can no longer hide new ones (no new News 10-12 Oct 2026).
            items = sources.collect(used, body, today, lookback, 10**6)
        except Exception as exc:
            stats.failed += 1
            stats.sources_failed += 1
            stats.errors.append(f"{src.id}: fetch/parse failed: {exc}")
            log.error("%s: fetch/parse failed: %s", src.id, exc)
            continue
        cap = int(src.max_items or limit)
        unseen = [i for i in items if i.url not in seen]
        already = len(items) - len(unseen)
        stats.duplicate += already
        items = unseen[:cap]
        stats.fetched += len(items) + already
        log.info("%s: %d item(s) after filters, %d already seen%s", src.id, len(items), already,
                 (" (via Bing News)" if "bing.com" in used.url else " (via Google News site search)") if used is not src else "")

        for item in items:
            if deadline is not None and time.monotonic() > deadline:
                stats.stopped_early = True
                break
            if item.url in seen:  # the same story from an earlier search in this run
                stats.duplicate += 1
                continue
            # Cheap checks first (no network): the animal-word guard and the same-title check.
            if needs_animal_word(item) and not policy_mod.about_animals(f"{item.title} {item.text}", policy):
                seen.add(item.url)
                stats.skipped += 1
                log.info("%s: skipped (no animal word) %s", src.id, item.url)
                save()
                continue
            if item.source.type in ("news", "legal") and title_seen(titles, item.title, item.date):
                seen.add(item.url)
                stats.duplicate += 1
                log.info("%s: duplicate title (same day ±%d) %s", src.id, TITLE_DAYS, item.url)
                save()
                continue
            original = item.url
            if enricher and not item.doc_kind:  # documents are PDFs: nothing to resolve or preview
                item.url, page_image = enricher(item.url)
                item.image = page_image or item.image  # the page's picture, else the one the feed gave
                if item.url != original and item.url in seen:
                    seen.add(original)
                    stats.duplicate += 1
                    continue
            why = "" if item.doc_kind else sources.not_an_article(item.url, item.title)
            if why:
                seen.add(item.url)
                stats.skipped += 1
                log.info("%s: skipped (%s) %s", src.id, why, item.url)
                save()
                continue
            if policy is not None:
                why = policy.verdict(item.url, src.id, f"{item.title} {item.text}")
                if why:
                    seen.add(item.url)
                    stats.skipped += 1
                    log.info("%s: skipped (%s) %s", src.id, why, item.url)
                    save()
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
                    if verdict is not None and verdict.same_as:  # same event: its facts and credit go to the first story
                        merge.fold(summariser, client, gate.kept_id(verdict.same_as), title=item.title, url=item.url,
                                   publisher=item.publisher or item.source.name, text=item.text, dry_run=dry_run,
                                   date=item.date)
                    save()
                    continue
            else:
                verdict = None
            try:
                as_press = item.source.file_as == "press"  # Media & PR coverage: no summary or categories
                summary = ("" if as_press else ingest.opportunity_summary(item) if item.source.type == "opportunity"
                           else summariser.summarise(item))
                cats = ([] if as_press else None if item.source.type == "opportunity"
                          else summarise.pick_categories(summariser, item, summary, sources.allowed_categories(item.source.type)))
                payload = ingest.build_payload(item, summary, cats)
                if summary and not as_press and item.source.type != "opportunity" and getattr(summariser, "last_numbers", None):  # a number not in the source: held for review (owner, 20 Oct 2026)
                    payload["hold"] = "; ".join(filter(None, [payload.get("hold", ""), *summariser.last_numbers]))
                if getattr(summariser, "last_short", False) and not as_press:
                    payload["summary_short"] = True  # still below the minimum after the fallback: published, marked
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
            last = getattr(client, "last", None) or {}
            if result == "created" and last.get("id") and not as_press:
                credit_coverage(client, int(last["id"]), getattr(summariser, "last_coverage", None))
            if result == "created" and gate is not None and last.get("id") and item.source.type in ("news", "legal") and not as_press:
                gate.recent_ids[item.title] = int(last["id"])  # later reports this run can be folded into it
            if not as_press and last.get("review") == "merged" and last.get("merged_into"):  # the site found the same event by title
                merge.fold(summariser, client, last["merged_into"], title=item.title, url=item.url,
                           publisher=item.publisher or item.source.name, text=item.text, date=item.date)
            if result == "duplicate":
                stats.duplicate += 1
            elif result in ("blocked", "filtered"):
                stats.skipped += 1
            else:
                stats.new += 1
                if (getattr(client, "last", None) or {}).get("review") == "published":
                    stats.published += 1
            save()
    return stats


def credit_coverage(client, item_id: int, reports) -> int:
    """The other reports a summary was written from (summarise.py's short fallback) are credited on the item as
    publisher logos in its source row (aw/v1/same-story, credit only). Returns how many were credited."""
    n = 0
    for r in reports or []:
        if not hasattr(client, "same_story"):
            break
        try:
            client.same_story(merge.payload(item_id, url=r.url, publisher=r.publisher, adds="", summary="", model=""))
            n += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("coverage credit not added to #%s: %s", item_id, exc)
    return n


def default_enricher(url: str) -> tuple[str, str]:
    """Publisher URL (resolving Google News links) and its preview image."""
    real = enrich.resolve_google_news(url)
    return real, enrich.og_image(real)


EXTRA_SEARCHES = ("stray dogs",)  # owner, 12 Oct 2026
SEARCH_MAX_ITEMS = 40  # new (unseen) items per Google News search and run; busy searches return 30-45 a day
FAIL_SHARE = 0.5  # half the sources or more failing makes the run fail and the site alert the owner
TIME_BUDGET_MIN = 25  # stop taking new items after this many minutes (job timeout 45); the next run carries on


def keyword_searches(source_list: list, lists: dict) -> list:
    """Search, don't only filter (owner, 12 Oct 2026): every English keyword in Sourcing (plus EXTRA_SEARCHES) becomes
    its own Google News search for the last 2 days, India edition, so stories from any publisher that use those words
    are found. Existing Google News searches get the same 2-day window (without it Google returns older "relevant"
    stories and nothing new). Hindi keywords are not searched (no Hindi content, owner 10 Oct 2026)."""
    import re as _re
    import urllib.parse as up
    from dataclasses import replace as _replace

    def with_window(url: str) -> str:
        parts = up.urlsplit(url)
        qs = up.parse_qs(parts.query, keep_blank_values=True)
        q = qs.get("q", [""])[0]
        if "when:" not in q:
            qs["q"] = [q + f" when:{WINDOW}"]
        return up.urlunsplit(parts._replace(query=up.urlencode(qs, doseq=True)))

    def norm_q(url: str) -> str:  # the search itself, whatever the parameter order or spacing
        q = up.parse_qs(up.urlsplit(url).query).get("q", [""])[0]
        return " ".join(q.lower().split())

    out = [(_replace(s, url=with_window(s.url), max_items=s.max_items or SEARCH_MAX_ITEMS)
            if "news.google.com/rss/search" in s.url else s) for s in source_list]
    have = {s.id for s in out}
    queries = {norm_q(s.url) for s in out if "news.google.com/rss/search" in s.url}
    kws = [k for k in (lists.get("keywords") or []) if isinstance(k, str) and k.isascii() and k.strip()]
    for kw in list(dict.fromkeys([*kws, *EXTRA_SEARCHES])):
        sid = "gnews-kw-" + _re.sub(r"[^a-z0-9]+", "-", kw.lower()).strip("-")
        url = "https://news.google.com/rss/search?" + up.urlencode({"q": f'"{kw}" India when:{WINDOW}', "hl": "en-IN", "gl": "IN", "ceid": "IN:en"})
        if sid in have or norm_q(url) in queries:  # the same search is already a source: fetch it once
            continue
        out.append(sources.Source(id=sid, name=f"Google News – {kw}", kind="rss", type="news", url=url, max_items=SEARCH_MAX_ITEMS))
        have.add(sid)
        queries.add(norm_q(url))
    return out


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    dry_run = truthy(os.environ.get("DRY_RUN"), default=True)

    # Every request (feeds, link resolving, page reads) trusts the intermediates some government sites do not send.
    os.environ.setdefault("REQUESTS_CA_BUNDLE", sources.ca_bundle())
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
    lists_from = "yaml"
    policy_client = client
    if policy_client is None and all(os.environ.get(k) for k in ("WP_BASE_URL", "WP_INGEST_USER", "WP_INGEST_APP_PASSWORD")):
        policy_client = ingest.Client(os.environ["WP_BASE_URL"], os.environ["WP_INGEST_USER"], os.environ["WP_INGEST_APP_PASSWORD"])
    if policy_client is not None:
        try:
            raw = policy_client.policy()
            policy = policy_mod.Policy.from_json(raw)
            log.info("policy: %d blocked publisher(s), relevance filter %s", len(policy.blocked), "on" if policy.relevance_on else "off")
            # The lists (feeds, Library and opportunity sources, categories, press queries) come from Settings → Sourcing
            # once "Workers use these lists" is on there; otherwise, or if the site is unreachable, from sources.yaml.
            if isinstance(raw.get("config"), dict) and raw["config"].get("sources"):
                lists = raw["config"]
                lists_from = "site"
                log.info("lists: from the site (%d sources)", len(lists["sources"]))
            else:
                log.info("lists: from sources.yaml")
            relay.configure(policy_client, raw.get("relay_hosts"))
            if raw.get("relay_hosts"):
                log.info("relay hosts (fetched through the site on 403): %s", ", ".join(raw["relay_hosts"]))
        except ingest.AuthError as exc:
            log.error("AUTH FAILURE, stopping: %s", exc)
            print(f"::error::Policy authentication failed: {exc}")
            return 3
        except Exception as exc:  # the site enforces the policy too, so carry on without the pre-filter
            log.warning("policy unavailable, relying on the site to enforce it: %s", exc)
    source_list, defaults = sources.load_sources_data(lists)
    source_list = keyword_searches(source_list, lists)
    # Opportunities are not fetched (owner, 9 Oct 2026). AW_FETCH_OPPORTUNITIES=1 would turn them back on.
    if not truthy(os.environ.get("AW_FETCH_OPPORTUNITIES"), default=False):
        source_list = [s for s in source_list if s.type != "opportunity"]
    gate = relevance.Gate.from_site(summariser, client or policy_client, dry_run=dry_run)
    summary_rules.use(client or policy_client)  # Settings → Sourcing → Summary rules; the defaults when unreachable

    def checkpoint():  # live runs keep their progress item by item (a run cut short no longer starts over)
        save_seen(SEEN_FILE, seen)
        save_titles(TITLES_FILE, titles, sources.utc_today())
        gate.save()

    budget = float(os.environ.get("AW_TIME_BUDGET_MIN") or TIME_BUDGET_MIN)
    try:
        stats = run(source_list, defaults, dry_run=dry_run, summariser=summariser, client=client, seen=seen,
                    enricher=default_enricher, policy=policy, titles=titles, gate=gate,
                    checkpoint=None if dry_run else checkpoint, deadline=time.monotonic() + budget * 60)
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
    gate.flush()  # the Rejected log, in one request
    if not dry_run:
        checkpoint()
        documents.save_state()  # High Court file versions: only remembered when items were really sent
    if ledger:
        SPEND_FILE.parent.mkdir(parents=True, exist_ok=True)
        SPEND_FILE.write_text(json.dumps(ledger))
        month = max(ledger)
        print(f"haiku spend {month}: ${ledger[month]:.4f} (cap ${float(os.environ.get('HAIKU_MONTHLY_BUDGET_USD') or 4):.2f})")

    print(f"fetched={stats.fetched} new={stats.new} published={stats.published} duplicate={stats.duplicate} "
          f"skipped={stats.skipped} failed={stats.failed} rejected={stats.rejected} classifier_unavailable={stats.unavailable} "
          f"sources={stats.sources} sources_failed={stats.sources_failed}" + (" stopped_early=1" if stats.stopped_early else ""))
    print(f"relevance gate: {gate.counts}")
    print(f"lists: {'the site (Settings → Sourcing)' if lists_from == 'site' else 'sources.yaml'}")
    sources_down = sources_failing(stats)
    if sources_down:
        print(f"::error::{stats.sources_failed} of {stats.sources} sources could not be fetched: " + "; ".join(stats.errors[:5])[:900])
    if client is not None and not dry_run:
        try:  # the site alerts the owner when nothing is published for 12 hours, or when half the sources fail
            client.ingest_heartbeat(stats.new, published=stats.published, sources=stats.sources,
                                    sources_failed=stats.sources_failed, errors=stats.errors[:10], lists=lists_from)
        except Exception as exc:
            log.warning("heartbeat not sent: %s", exc)
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
                f"### {'DRY_RUN' if dry_run else 'LIVE'} run (lists: {lists_from})\n\n| fetched | new | published | duplicate | skipped | rejected | classifier unavailable | failed | sources failed |\n|---|---|---|---|---|---|---|---|---|\n"
                f"| {stats.fetched} | {stats.new} | {stats.published} | {stats.duplicate} | {stats.skipped} | {stats.rejected} | {stats.unavailable} | {stats.failed} | {stats.sources_failed}/{stats.sources} |\n"
            )
    if sources_down:
        return 1  # red in Actions (GitHub emails the failure) as well as the site's alert
    return 1 if stats.failed and not stats.new and not stats.duplicate and not stats.skipped and not stats.rejected else 0


def sources_failing(stats: Stats) -> bool:
    """Half the sources or more could not be fetched (Google blocking GitHub's servers, a site down...)."""
    return stats.sources > 0 and stats.sources_failed >= FAIL_SHARE * stats.sources


if __name__ == "__main__":
    sys.exit(main())
