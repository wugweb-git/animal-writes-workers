"""Crawler-access check from outside the server (owner, 31 Oct 2026; Search Insights step 3).

On 26 Oct an SEO tool's crawler got no answer on 17 of 20 pages, while the same pages answered 200 from the owner's Mac
and from the server. A GitHub runner has an outside data-centre address, like that tool and the AI crawlers, so this job
asks the site's sample pages with each crawler's user agent and then a 20-page burst, and sends the results to POST
aw/v1/search-insights/crawl. The site raises a Site Health problem and an alert when a page is refused or answers with a
challenge page (aw-seo-extras, search-insights/crawlcheck.php); the server's own weekly self-test is the inside check.

The pages and user agents come from GET aw/v1/search-insights/crawl (the same list the server tests); when the site
cannot be asked (a dry run with no secrets, or the route not yet deployed) the public sitemaps give the pages.

DRY_RUN is on by default: it checks and prints the table, and sends nothing.
"""
from __future__ import annotations

import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable

import requests

from . import ingest

log = logging.getLogger("crawl_check")

TIMEOUT = 25
BURST_SIZE = 20
BURST_PARALLEL = 5
GAP = 1.0  # seconds between the sample-page requests of one agent

# The same agents as the site (crawlcheck.php aw_si_crawl_agents); the site's list wins when it can be read.
AGENTS = {
    "Browser": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Googlebot": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
    "Bingbot": "Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)",
    "GPTBot": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; GPTBot/1.2; +https://openai.com/gptbot)",
    "OAI-SearchBot": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; OAI-SearchBot/1.0; +https://openai.com/searchbot",
    "ChatGPT-User": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; ChatGPT-User/1.0; +https://openai.com/bot",
    "PerplexityBot": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; PerplexityBot/1.0; +https://perplexity.ai/perplexitybot)",
    "ClaudeBot": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; ClaudeBot/1.0; +claudebot@anthropic.com)",
    "Google-Extended": "Mozilla/5.0 (compatible; Google-Extended)",
    "Applebot": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/13.1.1 Safari/605.1.15 (Applebot/0.1; +http://www.apple.com/go/applebot)",
}
BURST_AGENTS = ("Browser", "GPTBot")  # a burst per agent would be 200 requests a week; these two stand for people and AI crawlers

# Sitemap fallback: kind => sitemap file (the first entry is used for the sample, the rest feed the burst).
SITEMAPS = {
    "listing": "aw_listing-sitemap1.xml",
    "news": "aw_news-sitemap1.xml",
    "library": "aw_legal-sitemap.xml",
    "resource": "post-sitemap.xml",  # Resources guides are posts under /resources/
    "opportunity": "aw_opportunity-sitemap.xml",
}
SM_LOC = "{http://www.sitemaps.org/schemas/sitemap/0.9}loc"
CHALLENGE = re.compile(r"captcha|cf-chl|challenge-platform|verify you are human|just a moment|access denied", re.I)


def _base() -> str:
    return (os.environ.get("WP_BASE_URL") or "https://animalwrites.in").rstrip("/")


def sitemap_pages(base: str, get: Callable = requests.get) -> dict:
    """{"pages": [{kind, url}], "burst": [url, ...]} from the public sitemaps."""
    pages, burst = [{"kind": "home", "url": base + "/"}], []
    for kind, name in SITEMAPS.items():
        try:
            r = get(f"{base}/{name}", timeout=TIMEOUT, headers={"User-Agent": AGENTS["Browser"]})
            locs = [e.text.strip() for e in ET.fromstring(r.content).iter() if e.tag == SM_LOC and e.text]  # not image:loc
        except Exception as exc:  # noqa: BLE001 - a missing sitemap only drops that kind
            log.warning("sitemap %s: %s", name, exc)
            continue
        if kind == "resource":
            locs = [u for u in locs if re.search(r"/resources/[^/]+/", u)]
        if locs:
            pages.append({"kind": kind, "url": locs[0]})
            burst += locs[1:4]
    burst = [p["url"] for p in pages] + burst
    return {"pages": pages, "burst": burst[:BURST_SIZE]}


def verdict(status: int, body: str) -> str:
    """ok, refused (not 200), or challenge (200, but a challenge page or not one of our pages)."""
    if status != 200:
        return "refused"
    if CHALLENGE.search(body[:20000]) and "animalwrites.in" not in body[:20000]:
        return "challenge"
    if 'rel="canonical"' not in body and "rel='canonical'" not in body:
        return "challenge"
    return "ok"


def fetch(url: str, ua: str, get: Callable = requests.get) -> dict:
    t = time.monotonic()
    try:
        r = get(url, timeout=TIMEOUT, headers={"User-Agent": ua}, allow_redirects=True)
        body = r.text or ""
        return {"code": r.status_code, "ms": int((time.monotonic() - t) * 1000), "bytes": len(body.encode()), "result": verdict(r.status_code, body)}
    except requests.RequestException as exc:
        return {"code": 0, "ms": int((time.monotonic() - t) * 1000), "bytes": 0, "result": "refused", "error": type(exc).__name__}


def run(plan: dict, agents: dict, get: Callable = requests.get, sleep: Callable = time.sleep) -> dict:
    checks = []
    for name, ua in agents.items():
        for i, p in enumerate(plan["pages"]):
            if i:
                sleep(GAP)
            checks.append({"agent": name, "kind": p["kind"], "url": p["url"], **fetch(p["url"], ua, get)})
    bursts = []
    for name in BURST_AGENTS:
        if name not in agents or not plan.get("burst"):
            continue
        with ThreadPoolExecutor(BURST_PARALLEL) as ex:
            got = list(ex.map(lambda u, ua=agents[name]: fetch(u, ua, get), plan["burst"]))
        codes: dict[str, int] = {}
        for g in got:
            codes[str(g["code"])] = codes.get(str(g["code"]), 0) + 1
        bursts.append({
            "agent": name,
            "n": len(got),
            "ok": sum(g["result"] == "ok" for g in got),
            "codes": codes,
            "max_ms": max((g["ms"] for g in got), default=0),
        })
    return {"runner": "github", "checks": checks, "burst": bursts}


def table(res: dict) -> str:
    rows = ["| Agent | OK | Problems |", "|---|---|---|"]
    for name in dict.fromkeys(c["agent"] for c in res["checks"]):
        mine = [c for c in res["checks"] if c["agent"] == name]
        bad = [f"{c['kind']} {c['code']} {c['result']}" for c in mine if c["result"] != "ok"]
        rows.append(f"| {name} | {len(mine) - len(bad)}/{len(mine)} | {', '.join(bad) or '—'} |")
    for b in res["burst"]:
        rows.append(f"| Burst ({b['agent']}, {b['n']} pages, {BURST_PARALLEL} at a time) | {b['ok']}/{b['n']} | codes {b['codes']}, slowest {b['max_ms']} ms |")
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    dry_run = str(os.environ.get("DRY_RUN", "true")).strip().lower() not in ("false", "0", "no", "off")
    client = None
    if os.environ.get("WP_BASE_URL") and os.environ.get("WP_INGEST_APP_PASSWORD"):
        client = ingest.Client(os.environ["WP_BASE_URL"], os.environ["WP_INGEST_USER"], os.environ["WP_INGEST_APP_PASSWORD"])
    elif not dry_run:
        log.error("WP_BASE_URL, WP_INGEST_USER and WP_INGEST_APP_PASSWORD are needed to send results")
        return 1
    plan, agents = None, dict(AGENTS)
    if client:
        try:
            got = client.crawl_check_plan()
            if got.get("pages"):
                plan = {"pages": got["pages"], "burst": got.get("burst") or [p["url"] for p in got["pages"]]}
                agents = got.get("agents") or agents
        except ingest.AuthError:
            raise
        except Exception as exc:  # noqa: BLE001 - the route may not be deployed yet
            log.warning("the site's page list is unavailable (%s): using the sitemaps", exc)
    plan = plan or sitemap_pages(_base())
    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    res = run(plan, agents)
    res["started"] = started
    out = table(res)
    print(out)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(f"## Crawler-access check{' (dry run: nothing sent)' if dry_run else ''}\n\n{out}\n")
    if dry_run:
        return 0
    try:
        log.info("site stored %s", client.crawl_check_post(res))
    except ingest.AuthError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.error("the site did not take the results: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
