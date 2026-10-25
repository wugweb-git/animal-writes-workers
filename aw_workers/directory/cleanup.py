"""Directory clean-up pass (owner, 25 Oct 2026): missing districts and states, and true duplicates, as PROPOSALS.

  python -m aw_workers.directory.run        with CLEANUP=true (workflow "Directory enrichment", input "cleanup")
  env: WP_BASE_URL, WP_INGEST_USER, WP_INGEST_APP_PASSWORD, DRY_RUN (default true), ENRICH_LIVE (the repository
       variable: anything but "true" keeps it a dry run), REPORT_DIR (cleanup-report),
       CLEANUP_FILE (optional: read the listings from this JSON instead of the site, for a local dry run),
       PIN_CACHE (optional: a JSON file of India Post answers kept between local runs)

Reads every published listing from aw/v1/enrich/cleanup (its place codes, its own place text and its contacts) and the
LGD states and districts the site uses. Nothing is changed here: each finding is sent to aw/v1/enrich/results as a
proposal with its source and date, and the site applies its own rules (fill-empty + keep-both; claimed or verified
listings, low confidence and every duplicate wait in Directory -> Enrichment review). A dry run sends nothing.

1. Place ("place" = {state, district} LGD codes), only from the listing's OWN details:
   - its PIN code (the PIN code field, or a PIN in its address) looked up at India Post; one district for every post
     office of the PIN = a candidate. The PIN's state must be the listing's state (else: low, for review);
   - otherwise exactly one district of its state named in its address / area / city text (medium);
   - a district named only in the listing's title: low (review);
   - a listing with no state: a state named in its own text (medium), with its district when one is named.
   1962 Mobile Veterinary Units are state-wide: they keep no district and are not counted as missing.
   A PIN found only in the address also fills an empty PIN code field (field "pincode").
2. Duplicates ("merge_into" = the listing kept): same name in the same district AND at least one of the same phone,
   email, website or address. The most complete listing is kept (a claimed or verified one first); the others get a
   "merge into #X" proposal, which the site never applies by itself. Same-name groups with nothing shared are reported
   for the owner and not proposed.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import time
from collections import Counter, defaultdict
from pathlib import Path

from .. import counts, ingest
from .model import Polite, fold, norm_phone, norm_url, now_ist, pincode_in

log = logging.getLogger("aw_workers.directory.cleanup")
INDIA_POST = "https://api.postalpincode.in/pincode/{}"
MVU_TYPE = "mobile-veterinary-unit"
GENERIC_PLACE_WORDS = {"north", "south", "east", "west", "central", "city", "district", "rural", "urban", "new"}
SOCIAL_HOSTS = ("facebook.com", "instagram.com", "twitter.com", "x.com", "youtube.com", "linkedin.com", "wa.me",
                "google.com", "goo.gl", "linktr.ee", "justdial.com")


def truthy(v, default: bool) -> bool:
    return default if v in (None, "") else str(v).lower() in ("1", "true", "yes", "on")


def key(name: str) -> str:
    """A place name for matching: folded, without "district" / "dist." and extra spaces."""
    s = fold(html.unescape(name or ""))
    s = re.sub(r"\b(district|dist|distt|zila|jilla)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


class Places:
    """The site's LGD states and districts (aw/v1/enrich/cleanup, "places"), with its spelling aliases."""

    def __init__(self, data: dict):
        self.states = {int(s["code"]): s["name"] for s in data.get("states", [])}
        self.districts = {int(d["code"]): (int(d["state"]), d["name"]) for d in data.get("districts", [])}
        al = data.get("aliases") or {}
        self.state_alias = {key(k): key(v) for k, v in (al.get("state") or {}).items()}
        self.district_alias = {key(k): key(v) for k, v in (al.get("district") or {}).items()}
        self.state_by_key = {key(n): c for c, n in self.states.items()}
        self.district_by_key: dict[str, list[int]] = defaultdict(list)
        for c, (_, n) in self.districts.items():
            self.district_by_key[key(n)].append(c)
        # Every spelling that names a district (its own name and the aliases that lead to it), for finding in text.
        self.spellings: dict[str, set[int]] = defaultdict(set)
        for k, codes in self.district_by_key.items():
            self.spellings[k].update(codes)
        for a, target in self.district_alias.items():
            self.spellings[a].update(self.district_by_key.get(target, []))
        self.state_spellings: dict[str, int] = dict(self.state_by_key)
        for a, target in self.state_alias.items():
            if target in self.state_by_key:
                self.state_spellings[a] = self.state_by_key[target]

    def state(self, name: str) -> int:
        k = key(name)
        return self.state_by_key.get(self.state_alias.get(k, k), 0)

    def district(self, name: str, state: int) -> int:
        k = key(name)
        for cand in (k, self.district_alias.get(k, "")):
            codes = [c for c in self.district_by_key.get(cand, []) if self.districts[c][0] == state]
            if len(codes) == 1:
                return codes[0]
        return 0

    def districts_in(self, text: str, state: int = 0) -> set[int]:
        """District codes named in a text (whole words), within one state when given. State names are taken out first
        ("Jammu and Kashmir" does not name the district Jammu), unless the state's name is also a district's."""
        hay = " " + key(text) + " "
        for sp in sorted(self.state_spellings, key=len, reverse=True):
            if " " in sp and sp not in self.spellings:
                hay = hay.replace(f" {sp} ", " , ")
        found: set[int] = set()
        for sp, codes in self.spellings.items():
            if len(sp) < 4 or sp in GENERIC_PLACE_WORDS or f" {sp} " not in hay:
                continue
            found |= {c for c in codes if not state or self.districts[c][0] == state}
        # "Mumbai Suburban" also contains "Mumbai": keep the longer name when both are found.
        names = {c: key(self.districts[c][1]) for c in found}
        return {c for c in found if not any(o != c and names[c] != names[o] and f" {names[c]} " in f" {names[o]} " for o in found)}

    def states_in(self, text: str) -> set[int]:
        hay = " " + key(text) + " "
        return {c for sp, c in self.state_spellings.items() if len(sp) >= 3 and f" {sp} " in hay}

    def label(self, state: int, district: int = 0) -> str:
        return ", ".join(x for x in ((self.districts.get(district) or (0, ""))[1], self.states.get(state, "")) if x)


class PinBook:
    """India Post's answer for a PIN code: the set of (state name, district name) of its post offices."""

    def __init__(self, http, cache_file: str | None = None):
        self.http, self.cache_file = http, cache_file
        self.cache: dict[str, list] = {}
        if cache_file and Path(cache_file).exists():
            try:
                self.cache = json.loads(Path(cache_file).read_text())
            except ValueError:
                self.cache = {}
        self.calls = 0

    def lookup(self, pin: str) -> list[tuple[str, str]]:
        if pin not in self.cache:
            try:
                r = self.http.get(INDIA_POST.format(pin), gap=1.0)
                data = r.json() if r.status_code == 200 else None
            except Exception:  # noqa: BLE001  India Post down: no answer, nothing proposed from the PIN
                return []
            self.calls += 1
            offices = ((data or [{}])[0] or {}).get("PostOffice") or []
            self.cache[pin] = sorted({(o.get("State") or "", o.get("District") or "") for o in offices})
        return [tuple(x) for x in self.cache[pin]]

    def save(self) -> None:
        if self.cache_file:
            Path(self.cache_file).write_text(json.dumps(self.cache, indent=0, sort_keys=True))


def own_text(L: dict) -> str:
    return " , ".join(x for x in (L.get("address"), L.get("import_address"), L.get("awbi_address"), L.get("area"), L.get("city")) if x)


def is_mvu(L: dict) -> bool:
    return MVU_TYPE in (L.get("types") or []) or html.unescape(L.get("name") or "").startswith("1962")


def src(key_: str, name: str, url: str = "") -> dict:
    return {"key": key_, "name": name, "url": url, "fetched_at": now_ist()}


def place_finding(L: dict, places: Places, pins: PinBook) -> dict | None:
    """The place proposal for a listing with no district (or no state), or None. Adds "why" for the report."""
    states = [int(s) for s in L.get("states") or []]
    if L.get("districts") or len(states) > 1:
        return None
    state = states[0] if states else 0
    text = own_text(L)
    pin_field = re.sub(r"\D", "", str(L.get("pincode") or ""))
    pin = pin_field if re.fullmatch(r"[1-9]\d{5}", pin_field) else pincode_in(text)
    by_pin = (0, 0)
    pin_states: set[int] = set()
    if pin:
        hits = set()
        for st_name, di_name in pins.lookup(pin):
            s = places.state(st_name)
            if s:
                pin_states.add(s)
                hits.add((s, places.district(di_name, s)))
        if len(hits) == 1:
            by_pin = next(iter(hits))
        elif len(pin_states) == 1:
            by_pin = (next(iter(pin_states)), 0)
    in_text = places.districts_in(text, state) if text else set()
    in_title = places.districts_in(L.get("name") or "", state) if not in_text else set()
    listing_src = src("listing", "The listing's own address and place", L.get("url") or "")
    pin_src = src("india_post", f"India Post PIN code directory ({pin})", INDIA_POST.format(pin)) if pin else None
    out = {"pin": pin, "pin_from_address": bool(pin and not pin_field)}

    if by_pin[0]:
        s, d = by_pin
        named = places.states_in(L.get("name") or "") | places.states_in(text)  # owner, 25 Oct 2026: the PIN never overrules the listing's own name or address
        if named and s not in named:
            return {**out, "value": {"state": s, "district": d}, "confidence": "low", "sources": [pin_src],
                    "why": f"PIN {pin} is in {places.label(s, d)}; the listing's own name or address says {', '.join(places.states.get(x, '') for x in sorted(named))}"}
        if state and s != state:
            return {**out, "value": {"state": s, "district": d}, "confidence": "low", "sources": [pin_src],
                    "why": f"PIN {pin} is in {places.label(s, d)}; the listing says {places.states.get(state)}"}
        if d:
            others = in_text - {d}
            if others:
                return {**out, "value": {"state": s, "district": d}, "confidence": "low", "sources": [pin_src, listing_src],
                        "why": f"PIN {pin} says {places.label(s, d)}; the address names {', '.join(places.label(*places.districts[o][:1], o) for o in sorted(others))}"}
            return {**out, "value": {"state": s, "district": d}, "confidence": "high",
                    "sources": [pin_src] + ([listing_src] if d in in_text else []), "why": f"PIN {pin}"}
        if not state:  # the PIN gives the state only
            return {**out, "value": {"state": s, "district": 0}, "confidence": "high", "sources": [pin_src], "why": f"PIN {pin} (state only)"}
    if len(in_text) == 1:
        d = next(iter(in_text))
        return {**out, "value": {"state": places.districts[d][0], "district": d}, "confidence": "medium",
                "sources": [listing_src], "why": "one district named in the address"}
    if len(in_text) > 1:
        return {**out, "ambiguous": sorted(in_text), "why": "several districts named in the address"}
    if len(in_title) == 1:
        d = next(iter(in_title))
        return {**out, "value": {"state": places.districts[d][0], "district": d}, "confidence": "low",
                "sources": [src("listing", "The listing's name", L.get("url") or "")], "why": "district named only in the listing's name"}
    if not state:
        st = places.states_in(text) or places.states_in(L.get("source_name") or "")
        if len(st) == 1:
            return {**out, "value": {"state": next(iter(st)), "district": 0}, "confidence": "medium", "sources": [listing_src],
                    "why": "state named in the listing's own details"}
    return {**out, "why": "nothing in the listing's own details names a district"} if text or pin else None


# ---------- duplicates ----------

def contact_keys(L: dict) -> set[tuple[str, str]]:
    keys = set()
    for p in L.get("phones") or []:
        d = re.sub(r"\D", "", norm_phone(str(p)) or "")
        if len(d) >= 10:
            keys.add(("phone", d[-10:]))
    for e in (L.get("email"), L.get("email_alt")):
        if e and "@" in e:
            keys.add(("email", e.strip().lower()))
    for w in (L.get("website"), L.get("website_alt")):
        u = re.sub(r"^https?://(www\.|m\.)?", "", norm_url(w or "").lower())
        if u and not u.startswith(SOCIAL_HOSTS):
            keys.add(("website", u))
    for a in (L.get("address"), L.get("import_address"), L.get("awbi_address")):
        k = key(a or "")
        if len(k) >= 15:
            keys.add(("address", k))
    return keys


def completeness(L: dict) -> int:
    return (len(L.get("phones") or []) + sum(1 for k in ("email", "website", "address", "import_address", "awbi_address", "pincode",
                                                         "awbi_code", "darpan_id", "facebook", "instagram") if L.get(k))
            + (1 if L.get("has_excerpt") else 0) + len(L.get("types") or []))


def duplicate_groups(items: list[dict]) -> tuple[list[dict], list[dict]]:
    """(true duplicate groups with the listing to keep, same-name groups left for the owner)."""
    by: dict[tuple, list[dict]] = defaultdict(list)
    for L in items:
        for d in L.get("districts") or []:
            n = re.sub(r"\s+", " ", fold(html.unescape(L.get("name") or ""))).strip()
            if n:
                by[(int(d), n)].append(L)
    merges, left = [], []
    for (district, name), group in sorted(by.items()):
        if len(group) < 2:
            continue
        parent = {L["id"]: L["id"] for L in group}

        def root(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i
        keys = {L["id"]: contact_keys(L) for L in group}
        shared: dict[tuple, set] = defaultdict(set)
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                common = keys[a["id"]] & keys[b["id"]]
                if common:
                    parent[root(a["id"])] = root(b["id"])
                    shared[tuple(sorted((a["id"], b["id"])))] |= {k for k, _ in common}
        comps: dict[int, list[dict]] = defaultdict(list)
        for L in group:
            comps[root(L["id"])].append(L)
        lone = [c[0] for c in comps.values() if len(c) == 1]
        for comp in (c for c in comps.values() if len(c) > 1):
            prot = [L for L in comp if L.get("protected")]
            if len(prot) > 1:
                left.append({"district": district, "name": name, "ids": sorted(L["id"] for L in comp),
                             "why": "two or more are claimed or verified"})
                continue
            keep = prot[0] if prot else sorted(comp, key=lambda L: (-completeness(L), L["id"]))[0]
            kinds = sorted({k for pair, ks in shared.items() if set(pair) & {L["id"] for L in comp} for k in ks})
            merges.append({"district": district, "name": name, "keep": keep, "others": [L for L in comp if L is not keep], "shared": kinds,
                           "pairs": {f"{a}-{b}": sorted(ks) for (a, b), ks in shared.items()}})
        if lone and (len(lone) > 1 or len(comps) > 1):
            codes = [L.get("awbi_code") for L in lone if L.get("awbi_code")]
            why = ("each has its own AWBI registration number: separate organisations" if len(codes) == len(lone) and len(set(codes)) == len(codes)
                   else "same name and district, but no shared phone, email, website or address")
            left.append({"district": district, "name": name, "ids": sorted(L["id"] for L in lone), "why": why})
    return merges, left


def merge_proposals(m: dict) -> list[tuple[dict, dict]]:
    """(listing, proposal) for each listing to merge into the kept one."""
    keep = m["keep"]
    out = []
    for L in m["others"]:
        if L.get("protected"):
            continue  # a claimed or verified listing is never merged away (the keep rule already prefers it)
        mine = {k for pair, ks in m["pairs"].items() if str(L["id"]) in pair.split("-") for k in ks}
        same_awbi = L.get("awbi_code") and L.get("awbi_code") == keep.get("awbi_code")
        conf = "high" if len(mine) >= 2 or same_awbi else "medium"
        out.append((L, {"field": "merge_into", "value": int(keep["id"]), "confidence": conf,
                        "sources": [src("listing", f"Same name and district as #{keep['id']}; same {', '.join(sorted(mine))}", keep.get("url") or "")]}))
    return out


# ---------- the run ----------

def fetch(client, env) -> tuple[list[dict], dict]:
    if env.get("CLEANUP_FILE"):
        data = json.loads(Path(env["CLEANUP_FILE"]).read_text())
        return data["items"], data["places"]
    items, places, page = [], {}, 1
    while True:
        r = client._call("GET", "/enrich/cleanup", params={"page": page}, timeout=120)
        items += r.get("items") or []
        places = places or r.get("places") or {}
        if page >= int(r.get("pages") or 1):
            return items, places
        page += 1


def summary(items: list[dict]) -> dict:
    sd = [L for L in items if L.get("states") and not L.get("districts")]
    merges, left = duplicate_groups(items)
    same = Counter()
    for L in items:
        for d in L.get("districts") or []:
            same[(int(d), re.sub(r"\s+", " ", fold(html.unescape(L.get("name") or ""))).strip())] += 1
    return {"published": len(items), "state_no_district": len(sd), "state_wide_1962": sum(1 for L in sd if is_mvu(L)),
            "missing_district": sum(1 for L in sd if not is_mvu(L)), "no_state": sum(1 for L in items if not L.get("states")),
            "same_name_groups": sum(1 for n in same.values() if n > 1), "same_name_listings": sum(n for n in same.values() if n > 1),
            "true_duplicate_groups": len(merges), "to_merge": sum(len(m["others"]) for m in merges), "left_groups": len(left)}


def main(env=None) -> int:
    env = dict(env or os.environ)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    dry = truthy(env.get("DRY_RUN"), True) or not truthy(env.get("ENRICH_LIVE"), False)  # live only when ENRICH_LIVE is "true" too
    client = None if env.get("CLEANUP_FILE") and dry else ingest.Client(env["WP_BASE_URL"], env["WP_INGEST_USER"], env["WP_INGEST_APP_PASSWORD"])
    items, raw_places = fetch(client, env)
    on = {} if client is None else (client._call("GET", "/enrich/config", timeout=60).get("sources") or {})
    places = Places(raw_places)
    pins = PinBook(Polite(gap=1.0), env.get("PIN_CACHE"))
    run_id = "cleanup-" + (env.get("GITHUB_RUN_ID") or str(int(time.time())))
    before = summary(items)

    per_listing: dict[int, list[dict]] = defaultdict(list)
    place_rows, unresolved = [], []
    for L in items:
        if is_mvu(L) or not on.get("listing", True):
            continue
        f = place_finding(L, places, pins) if (not L.get("districts")) else None
        if not f:
            continue
        uses_pin = any(s["key"] == "india_post" for s in f.get("sources") or [])
        if "value" in f and not (uses_pin and not on.get("india_post", True)):
            per_listing[L["id"]].append({"field": "place", "value": f["value"], "confidence": f["confidence"], "sources": f["sources"]})
            place_rows.append((L, f))
        else:
            unresolved.append((L, f))
        if f.get("pin_from_address") and not L.get("pincode") and "value" in f and f["confidence"] != "low":
            per_listing[L["id"]].append({"field": "pincode", "value": f["pin"], "confidence": "medium",
                                         "sources": [src("listing", "PIN code in the listing's own address", L.get("url") or "")]})
    pins.save()

    merges, left = duplicate_groups(items)
    for m in merges:
        for L, p in merge_proposals(m):
            per_listing[L["id"]].append(p)

    outcome: dict[str, Counter] = defaultdict(Counter)
    for lid, props in per_listing.items():
        if dry:
            for p in props:
                kind = "queued" if p["field"] == "merge_into" or p["confidence"] == "low" else "applied"
                outcome[p["field"]][kind] += 1
            continue
        r = client._call("POST", "/enrich/results", timeout=120, json={"listing_id": lid, "run": run_id, "import": True, "proposals": props})
        for kind in ("applied", "queued", "skipped"):
            for f in r.get(kind, []):
                outcome[f][kind] += 1

    out = Path(env.get("REPORT_DIR") or "cleanup-report")
    out.mkdir(parents=True, exist_ok=True)
    md = report(dry, before, place_rows, unresolved, merges, left, outcome, places, pins.calls)
    (out / "report.md").write_text(md)
    (out / "report.json").write_text(json.dumps({
        "dry_run": dry, "before": before, "outcome": outcome,
        "place": [{"id": L["id"], "name": L.get("name"), "value": f["value"], "confidence": f["confidence"], "why": f["why"]} for L, f in place_rows],
        "unresolved": [{"id": L["id"], "name": L.get("name"), "why": f.get("why"), "ambiguous": f.get("ambiguous")} for L, f in unresolved],
        "merges": [{"district": m["district"], "name": m["name"], "keep": m["keep"]["id"], "merge": [L["id"] for L in m["others"]], "shared": m["shared"]} for m in merges],
        "left_for_owner": left,
        "state_wide_1962": [L["id"] for L in items if is_mvu(L) and L.get("states") and not L.get("districts")],
    }, indent=1, default=dict))
    if env.get("GITHUB_STEP_SUMMARY"):
        with open(env["GITHUB_STEP_SUMMARY"], "a") as fh:
            fh.write(md.split("## Place proposals")[0])
    print(md.split("## Place proposals")[0])
    counts.write(listings=len(per_listing), place_proposals=len(place_rows), merge_proposals=sum(len(m["others"]) for m in merges), dry_run=dry)
    return 0


def report(dry, before, place_rows, unresolved, merges, left, outcome, places, pin_calls) -> str:
    conf = Counter(f["confidence"] for _, f in place_rows)
    lines = [f"# Directory clean-up {'dry run' if dry else 'run'}, {now_ist()[:16].replace('T', ' ')} IST", "",
             f"{before['published']} published listings. India Post lookups: {pin_calls}.", "",
             "## Before", "",
             f"- State but no district: {before['state_no_district']} ({before['state_wide_1962']} are state-wide 1962 units, not counted as missing: {before['missing_district']})",
             f"- No state: {before['no_state']}",
             f"- Same name in the same district: {before['same_name_groups']} groups ({before['same_name_listings']} listings); "
             f"true duplicates (a shared phone, email, website or address): {before['true_duplicate_groups']} groups, {before['to_merge']} listings to merge",
             "", "## Proposals", "",
             f"- Place: {len(place_rows)} (high {conf['high']}, medium {conf['medium']}, low {conf['low']}); nothing usable for {len(unresolved)}",
             f"- Merge into another listing: {sum(len(m['others']) for m in merges)} (always reviewed by the owner)",
             f"- Same-name groups left for the owner: {len(left)}", "",
             "| Field | " + ("Would apply | Would queue" if dry else "Applied | Queued | Skipped") + " |", "|---|---|---|" + ("" if dry else "---|")]
    for f, c in sorted(outcome.items()):
        lines.append(f"| {f} | {c['applied']} | {c['queued']} |" + ("" if dry else f" {c['skipped']} |"))
    lines += ["", "## Place proposals", ""]
    for L, f in place_rows:
        v = f["value"]
        lines.append(f"- [{html.unescape(L.get('name') or '')}]({L.get('url')}) #{L['id']}: {places.label(v['state'], v['district'])} ({f['confidence']}; {f['why']})")
    lines += ["", "## Merge proposals", ""]
    for m in merges:
        lines.append(f"- {m['name']} ({places.label(places.districts.get(m['district'], (0,))[0], m['district'])}): keep #{m['keep']['id']}, merge "
                     + ", ".join(f"#{L['id']}" for L in m["others"]) + f" (same {', '.join(m['shared'])})")
    lines += ["", "## Same name, left for the owner", ""]
    for g in left:
        lines.append(f"- {g['name']} ({places.label(places.districts.get(g['district'], (0,))[0], g['district'])}): " + ", ".join(f"#{i}" for i in g["ids"]) + f" ({g['why']})")
    lines += ["", "## No district found", ""]
    for L, f in unresolved:
        lines.append(f"- #{L['id']} {html.unescape(L.get('name') or '')}: {f.get('why')}")
    return "\n".join(lines) + "\n"
