"""Directory enrichment run (owner's brief, 18 Oct 2026).

  python -m aw_workers.directory.run
  env: WP_BASE_URL, WP_INGEST_USER, WP_INGEST_APP_PASSWORD, the AI keys, GOOGLE_PLACES_KEY,
       GOOGLE_PLACES_FREE_REQUESTS (Google's free monthly requests we may use, no charge; default 0),
       GOOGLE_PLACES_MONTHLY_BUDGET_USD (paid spend on top; default 0: free quota only, owner 20 Oct 2026), DRY_RUN (default true), BATCH (50), GROUP
       ("awbi-bare" = only AWBI listings with no phone, email or website), AW_IDS (1,2,3), MAX_MINUTES (150),
       REPORT_DIR (enrich-report), CLEANUP ("true" = the clean-up pass instead, cleanup.py)

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

from .. import counts, ingest, summarise
from . import ai, cleanup, lead_finder, merge
from .model import Context, Polite, now_ist
from .sources import REGISTRY, google_places

log = logging.getLogger("aw_workers.directory")
SWEEP_MIN = 3  # listings of one district in a batch before a district search is made


def truthy(v: str | None, default: bool) -> bool:
    return default if v in (None, "") else str(v).lower() in ("1", "true", "yes", "on")


LEADS_FILE = Path(__file__).resolve().parents[2] / "data" / "directory-leads.json"


def file_leads(listing_id: int) -> dict:
    """Leads found by searching Google in a browser (data/directory-leads.json): the organisation's own pages."""
    try:
        return {k: v for k, v in (json.loads(LEADS_FILE.read_text()).get(str(listing_id)) or {}).items() if v}
    except (OSError, ValueError):
        return {}


def enrich_one(listing: dict, env: dict, http, on: dict, summariser, services: dict) -> Context:
    ctx = Context(listing)
    ctx.leads.update(listing.get("_asked") or {})  # Gemini's leads (lead_finder.py)
    ctx.leads.update(file_leads(listing["id"]))
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
    if truthy(env.get("CLEANUP"), False):  # the clean-up pass (cleanup.py): missing places and true duplicates, as proposals
        return cleanup.main(env)
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
    reason, google_note = "", ""
    sweeps, taken, carry = {}, {}, 0.0
    while done < batch and time.time() < stop_at:
        params = {"limit": min(200, batch) if dry else min(25, batch - done)}  # a dry run marks nothing done: one fetch
        if env.get("AW_IDS"):
            params["ids"] = env["AW_IDS"]
        if env.get("GROUP"):
            params["group"] = env["GROUP"]
        params["google"] = 1 if env.get("GOOGLE_PLACES_KEY") else 0  # listings checked while Google was off come back first once it is on
        q = client._call("GET", "/enrich/queue", params=params, timeout=120)
        items = q.get("items") or []
        if dry and done:
            break
        if not items:
            reason = reason or "queue empty"
            break
        need = [L for L in items if not file_leads(L["id"]) and not (L.get("now") or {}).get("website") and not (L.get("claim") or {}).get("aw_website")]
        gemini = [k for k in (env.get("GEMINI_API_KEY"), env.get("GEMINI_API_KEY_2")) if k]
        asked = lead_finder.find(need, gemini, http) if need and gemini and on.get("website", True) and env.get("LEAD_FINDER") == "1" else {}  # off: leads come from searches in the browser (owner, 21 Oct 2026; no Gemini billing)
        if asked:
            log.info("lead finder: %d of %d listings got a lead", sum(1 for v in asked.values() if v), len(need))
        if env.get("GOOGLE_PLACES_KEY") and on.get("google_places", True):  # one search per district for all its listings
            by_district = Counter((L.get("state"), L.get("district")) for L in items if L.get("state") and L.get("district"))
            for (st, di) in {k for k, n in by_district.items() if n >= SWEEP_MIN} - set(sweeps):  # 2 requests pay off only for a few listings or more
                if float(q.get("google_spent") or 0) + spent_run + carry + 2 * google_places.COST_USD > limit:
                    break
                sweeps[(st, di)], used = google_places.sweep(env, http, st, di)
                carry += used * google_places.COST_USD  # reported to the site with the next listing's cost
        for listing in items:
            if done >= batch or time.time() >= stop_at:
                break
            listing["_district_places"] = sweeps.get((listing.get("state"), listing.get("district")), [])
            listing["_taken"] = taken.setdefault((listing.get("state"), listing.get("district")), set())
            listing["_asked"] = asked.get(listing["id"], {})
            if env.get("GOOGLE_PLACES_KEY") and float(q.get("google_spent") or 0) + spent_run + google_places.COST_USD > limit + 1e-9:
                google_note = (f"Google free quota ({free} requests) used for this month" if budget <= 0
                               else f"Google limit ({free} free requests + ${budget:.0f}) reached for this month")
                env["GOOGLE_PLACES_KEY"] = ""  # the run goes on without Google (the other sources still work); it is back next month
                log.info("%s: carrying on without Google", google_note)
            ctx = enrich_one(listing, env, http, on, summariser, services)
            ctx.google_cost += carry
            carry = 0.0
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
                    "google_skipped": not env.get("GOOGLE_PLACES_KEY") and bool(cfg.get("sources", {}).get("google_places", True)),
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
    md = report.markdown(f"Google requests this run: {google_n}" + (" (free quota, no charge)." if budget <= 0 else f" (${spent_run:.2f}).") + (f" {google_note}: the rest of the run went on without Google." if google_note else "") + (f" Stopped: {reason}." if reason and reason != "queue empty" else ""))
    (out / "report.md").write_text(md)
    (out / "report.json").write_text(json.dumps({"dry_run": dry, "listings": report.listings,
                                                 "fields": report.field, "sources": report.source}, indent=1, default=dict))
    if env.get("GITHUB_STEP_SUMMARY"):
        with open(env["GITHUB_STEP_SUMMARY"], "a") as fh:
            fh.write(md.split("## Listings")[0])
    print(md.split("## Listings")[0])
    counts.write(listings=done, google_requests=google_n, dry_run=dry)
    return 0


if __name__ == "__main__":
    sys.exit(main())
