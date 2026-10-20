"""Directory enrichment run (owner's brief, 18 Oct 2026).

  python -m aw_workers.directory.run
  env: WP_BASE_URL, WP_INGEST_USER, WP_INGEST_APP_PASSWORD, the AI keys, GOOGLE_PLACES_KEY,
       GOOGLE_PLACES_FREE_REQUESTS (Google's free monthly requests we may use, no charge; default 0),
       GOOGLE_PLACES_MONTHLY_BUDGET_USD (paid spend on top; default 0: free quota only, owner 20 Oct 2026), DRY_RUN (default true), BATCH (50), GROUP
       ("awbi-bare" = only AWBI listings with no phone, email or website), AW_IDS (1,2,3), MAX_MINUTES (150),
       REPORT_DIR (enrich-report)

The site picks the listings (aw/v1/enrich/queue: new or claimed first, then AWBI address-only, then the rest, then any
not checked for 90 days) and keeps which are done, so every run continues where the last stopped. Per listing: each
switched-on source in turn (sources/), the AI pass, the confidence merge, then one POST to aw/v1/enrich/results, where
the site applies its rules. A dry run sends nothing and reports what would happen. Google stops for the month at the
budget; the run then stops (Google is the locator), and the next month continues.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

from .. import ingest, summarise
from . import ai, merge
from .model import Context, Polite, now_ist
from .sources import REGISTRY, google_places

log = logging.getLogger("aw_workers.directory")


def truthy(v: str | None, default: bool) -> bool:
    return default if v in (None, "") else str(v).lower() in ("1", "true", "yes", "on")


def enrich_one(listing: dict, env: dict, http, on: dict, summariser, services: dict) -> Context:
    ctx = Context(listing)
    for key, mod in REGISTRY.items():
        if not on.get(key, True):
            ctx.status[key] = "off"
            continue
        try:
            mod.run(ctx, env, http)
        except Exception as exc:  # noqa: BLE001  one source failing never stops the others
            ctx.status[key] = f"error: {type(exc).__name__}: {str(exc)[:100]}"
    try:
        ai.run(ctx, summariser, services)
    except Exception as exc:  # noqa: BLE001
        ctx.status["ai"] = f"error: {type(exc).__name__}"
    return ctx


class Report:
    def __init__(self, dry: bool):
        self.dry, self.listings = dry, []
        self.field = defaultdict(Counter)  # field -> found / applied / queued / skipped
        self.source = defaultdict(Counter)  # source -> status kind, and "gave" (listings with at least one value)
        self.started = time.time()

    def add(self, listing, ctx, props, invite, outcome: dict):
        for k, st in ctx.status.items():
            self.source[k][st.split(":")[0]] += 1
        for k in {f.source_key for f in ctx.facts if f.role == "store"}:
            self.source[k]["gave"] += 1
        for f in {p["field"] for p in props}:
            self.field[f]["found"] += 1
        for kind in ("applied", "queued", "skipped"):
            for f in outcome.get(kind, []):
                self.field[f][kind] += 1
        self.listings.append({"id": listing["id"], "name": listing["name"], "url": listing.get("url"),
                              "place": ", ".join(x for x in (listing.get("district"), listing.get("state")) if x),
                              "status": ctx.status, "invite_claim": invite, "closed": bool(getattr(ctx, "closed", None)),
                              "proposals": [{**p, "outcome": next((k for k in ("applied", "queued", "skipped") if p["field"] in outcome.get(k, [])), "")} for p in props]})

    def markdown(self, extra: str = "") -> str:
        n = len(self.listings) or 1
        lines = [f"# Directory enrichment {'dry run' if self.dry else 'run'}, {now_ist()[:16].replace('T', ' ')} IST", "",
                 f"{len(self.listings)} listings · {round((time.time() - self.started) / 60, 1)} min. {extra}".strip(), "",
                 "## Per field", "", "| Field | Found | " + ("Would apply | Would queue" if self.dry else "Applied | Queued") + " | Already held |",
                 "|---|---|---|---|---|"]
        for f, c in sorted(self.field.items(), key=lambda x: -x[1]["found"]):
            lines.append(f"| {f} | {c['found']} ({round(100 * c['found'] / n)}%) | {c['applied']} | {c['queued']} | {c['skipped']} |")
        lines += ["", "## Per source", "", "| Source | Gave values | Status counts |", "|---|---|---|"]
        for s, c in sorted(self.source.items()):
            st = ", ".join(f"{k} {v}" for k, v in c.items() if k != "gave")
            lines.append(f"| {s} | {c['gave']} ({round(100 * c['gave'] / n)}%) | {st} |")
        lines += ["", "## Listings", ""]
        for L in self.listings:
            got = ", ".join(f"{p['field']} ({p['confidence']}, {'+'.join(sorted({s['key'] for s in p['sources']}))}){' → ' + p['outcome'] if p['outcome'] else ''}" for p in L["proposals"]) or "nothing found"
            flags = (" · Invite to claim: " + ", ".join(L["invite_claim"]) if L["invite_claim"] else "") + (" · Reported closed" if L["closed"] else "")
            lines.append(f"- [{L['name']}]({L['url']}) ({L['place']}): {got}{flags}")
        return "\n".join(lines) + "\n"


def main(env=None) -> int:
    env = dict(env or os.environ)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    dry = truthy(env.get("DRY_RUN"), True)
    batch = int(env.get("BATCH") or 50)
    stop_at = time.time() + 60 * float(env.get("MAX_MINUTES") or 150)
    # Google: Google's free monthly requests only (owner, 20 Oct 2026: 0 USD), plus any paid budget set on top (none).
    free = max(0, int(env.get("GOOGLE_PLACES_FREE_REQUESTS") or 0))
    budget = float(env.get("GOOGLE_PLACES_MONTHLY_BUDGET_USD") or 0)
    limit = budget + free * google_places.COST_USD
    if limit <= 0:
        env["GOOGLE_PLACES_KEY"] = ""  # no free requests or budget set: Google stays off
    client = ingest.Client(env["WP_BASE_URL"], env["WP_INGEST_USER"], env["WP_INGEST_APP_PASSWORD"])
    cfg = client._call("GET", "/enrich/config", timeout=60)
    on, services = cfg.get("sources") or {}, cfg.get("services") or {}
    try:
        summariser = summarise.Summariser(summarise.backends_from_env(env))
    except ValueError:
        summariser = None
    http = Polite(gap=1.0)
    report, run_id, spent_run, done = Report(dry), env.get("GITHUB_RUN_ID") or f"local-{int(time.time())}", 0.0, 0
    reason = ""
    while done < batch and time.time() < stop_at:
        params = {"limit": min(200, batch) if dry else min(25, batch - done)}  # a dry run marks nothing done: one fetch
        if env.get("AW_IDS"):
            params["ids"] = env["AW_IDS"]
        if env.get("GROUP"):
            params["group"] = env["GROUP"]
        q = client._call("GET", "/enrich/queue", params=params, timeout=120)
        items = q.get("items") or []
        if dry and done:
            break
        if not items:
            reason = reason or "queue empty"
            break
        for listing in items:
            if done >= batch or time.time() >= stop_at:
                break
            if env.get("GOOGLE_PLACES_KEY") and float(q.get("google_spent") or 0) + spent_run + google_places.COST_USD > limit + 1e-9:
                reason = (f"Google free quota ({free} requests) used for this month" if budget <= 0
                          else f"Google limit ({free} free requests + ${budget:.0f}) reached for this month")
                break
            ctx = enrich_one(listing, env, http, on, summariser, services)
            spent_run += ctx.google_cost
            props, invite = merge.proposals(ctx)
            flags = {"invite_claim": invite}
            if getattr(ctx, "closed", None):
                flags["reported_closed"] = ctx.closed
            if dry:
                outcome = defaultdict(list)
                for p in props:
                    outcome[merge.would(listing, p)].append(p["field"])
            else:
                outcome = client._call("POST", "/enrich/results", timeout=120, json={
                    "listing_id": listing["id"], "run": run_id, "google_cost": ctx.google_cost,
                    "sources_used": [k for k, v in ctx.status.items() if v != "off"],
                    "proposals": [{k: p[k] for k in ("field", "value", "confidence", "sources")} for p in props], "flags": flags})
            report.add(listing, ctx, props, invite, outcome)
            done += 1
            log.info("%s %s: %s", listing["id"], listing["name"][:50], ", ".join(f"{p['field']}:{p['confidence'][0]}" for p in props) or "nothing")
        if reason or env.get("AW_IDS"):
            break
    out = Path(env.get("REPORT_DIR") or "enrich-report")
    out.mkdir(parents=True, exist_ok=True)
    google_n = round(spent_run / google_places.COST_USD)
    md = report.markdown(f"Google requests this run: {google_n}" + (" (free quota, no charge)." if budget <= 0 else f" (${spent_run:.2f}).") + (f" Stopped: {reason}." if reason and reason != "queue empty" else ""))
    (out / "report.md").write_text(md)
    (out / "report.json").write_text(json.dumps({"dry_run": dry, "listings": report.listings,
                                                 "fields": report.field, "sources": report.source}, indent=1, default=dict))
    if env.get("GITHUB_STEP_SUMMARY"):
        with open(env["GITHUB_STEP_SUMMARY"], "a") as fh:
            fh.write(md.split("## Listings")[0])
    print(md.split("## Listings")[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
