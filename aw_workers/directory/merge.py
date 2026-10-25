"""From facts to proposals, with confidence (owner's brief):
high   = two or more sources agree, or the organisation's claim, or a Google match confirming a stored value;
medium = one source (not Google);
low    = sources disagree.
Google values are never proposed (except the place ID and Maps link); a field only Google has is "Invite to claim".
Values read by the AI are at most medium.

Field rules (sourcing restructure phase 3, owner 25 Oct 2026): the site sends them with GET enrich/config, edited in
Settings → Sourcing → Rules → Directory enrichment: `field_rules` {field: {on, sources, multi, official, ai, invite,
social}} and `priority` (the trust order). The constants below are the fallback when the site sends none, and the site
seeded its rules from them (tests/fixtures/enrich_field_rules_seed.json is the same file in both repos)."""

from __future__ import annotations

import logging

from .model import CHECK, STORE, same_value

log = logging.getLogger("aw_workers.directory.merge")

MULTI = {"phone": 2, "whatsapp": 1, "emergency": 1}  # how many different numbers may be proposed
AI_FIELDS = {"services", "animals", "areas_served", "description", "reg_type", "head"}
PRIORITY = ["claim", "website", "darpan", "wikidata", "osm", "social", "awbi", "google_places"]
INVITE_FIELDS = ("phone", "hours", "address", "website")
SOCIAL_FIELDS = {"instagram", "facebook", "x", "youtube", "linkedin"}
OFFICIAL_AWBI = {"address", "pincode"}  # the AWBI list is the official record of these: high on its own

# One field's rule. multi: 0 = one value, and sources that disagree make it low; n = up to n different values, never
# low for disagreeing (phones). official: a source whose value alone is high (AWBI for the address). ai: values the AI
# reads stay at most medium. invite: "Invite to claim" when only Google has it. social: a profile link, where the
# website and the page preview count as one source. sources: the sources that may fill it ([] = any). on: false = never
# proposed.
DEFAULT_RULE = {"on": True, "sources": [], "multi": 0, "official": "", "ai": False, "invite": False, "social": False}


def rule_from_constants(field: str) -> dict:
    """A field's rule as the constants above give it (what the site seeded on 25 Oct 2026)."""
    return {**DEFAULT_RULE, "multi": MULTI.get(field, 0), "official": "awbi" if field in OFFICIAL_AWBI else "",
            "ai": field in AI_FIELDS, "invite": field in INVITE_FIELDS, "social": field in SOCIAL_FIELDS}


def _clean_rule(raw) -> dict:
    """A rule from the site, each value checked; a missing or unusable value falls back to the default."""
    r = dict(DEFAULT_RULE)
    if not isinstance(raw, dict):
        return r
    for k in ("on", "ai", "invite", "social"):
        if isinstance(raw.get(k), bool):
            r[k] = raw[k]
    try:
        r["multi"] = max(0, int(raw.get("multi", 0)))
    except (TypeError, ValueError):
        pass
    if isinstance(raw.get("official"), str):
        r["official"] = raw["official"].strip()
    if isinstance(raw.get("sources"), list):
        r["sources"] = [str(x) for x in raw["sources"] if str(x).strip()]
    return r


class Rules:
    """The field rules and trust order in force for a run."""

    def __init__(self, fields: dict | None = None, priority: list | None = None, from_site: bool = False):
        self.fields = fields  # None: the constants
        self.priority = list(priority) if priority else list(PRIORITY)
        self.from_site = from_site

    def rule(self, field: str) -> dict:
        if self.fields is None or field not in self.fields:
            return rule_from_constants(field)  # a field the site has no rule for: as the constants say
        return self.fields[field]


_active = Rules()


def rules_from_config(cfg: dict | None) -> Rules:
    """The site's field rules and trust order from GET enrich/config; the constants when it sends none."""
    cfg = cfg if isinstance(cfg, dict) else {}
    fr = cfg.get("field_rules")
    pr = cfg.get("priority")
    priority = [str(x) for x in pr if str(x).strip()] if isinstance(pr, list) and pr else None
    if not isinstance(fr, dict) or not fr:
        return Rules(None, priority, from_site=priority is not None)
    return Rules({str(k): _clean_rule(v) for k, v in fr.items()}, priority, from_site=True)


def use_config(cfg: dict | None) -> Rules:
    """Use the site's rules for this run (the constants when it sends none); proposals() reads them."""
    global _active
    _active = rules_from_config(cfg)
    log.info("field rules: %s", "from the site" if _active.fields is not None else "built in (merge.py)")
    return _active


def active() -> Rules:
    return _active


def reset() -> None:
    global _active
    _active = Rules()


def _groups(facts, field):
    groups: list[list] = []
    for f in facts:
        for g in groups:
            if same_value(field, g[0].value, f.value):
                g.append(f)
                break
        else:
            groups.append([f])
    return groups


def proposals(ctx, rules: Rules | None = None) -> tuple[list[dict], list[str]]:
    """[{field, value, confidence, sources, note}], and the fields only Google has (Invite to claim). The field rules
    and trust order are the site's (use_config) or, without them, the constants."""
    rules = rules or _active
    order = rules.priority
    by: dict[str, dict[str, list]] = {}
    for f in ctx.facts:
        by.setdefault(f.field, {}).setdefault(f.role, []).append(f)
    out, invite = [], []
    for field, d in by.items():
        rule = rules.rule(field)
        if not rule["on"]:
            continue  # switched off in Rules → Directory enrichment: never proposed
        store, check = d.get(STORE, []), d.get(CHECK, [])
        if rule["sources"]:
            store = [f for f in store if f.source_key in rule["sources"]]  # only the sources allowed to fill it
        if not store:
            if check and rule["invite"] and not (ctx.listing.get("now") or {}).get(field):
                invite.append(field)  # only Google has it
            continue
        groups = _groups(store, field)
        groups.sort(key=lambda g: (-any(f.source_key == "claim" for f in g), -len({f.source_key for f in g}),
                                   min(order.index(f.source_key) if f.source_key in order else 99 for f in g)))
        keep = groups[: rule["multi"] or 1]
        rivals = groups[len(keep):]
        for g in keep:
            keys = {f.source_key for f in g}
            if rule["social"] and "website" in keys:
                keys.discard("social")  # the website links to the page and the preview reads that same page: one source, not two
            confirmed = any(same_value(field, c.value, g[0].value) for c in check)
            if "claim" in keys or (rule["official"] and keys == {rule["official"]}):
                conf = "high"  # the organisation itself, or the AWBI list's own address (owner, 18 Oct 2026)
            elif field in ("place_id", "maps_url"):
                conf = "high" if getattr(ctx, "google_match", 0) >= 0.85 else "medium"
            elif len(keys) >= 2 or confirmed:
                conf = "high"
            elif not rule["multi"] and any({f.source_key for f in x} - keys for x in rivals):
                conf = "low"  # another source says something else
            else:
                conf = "medium"
            if rule["ai"] and "claim" not in keys and conf == "high":
                conf = "medium"
            refs = list({(f.source_key, f.source_url): f.ref() for f in g}.values())
            if confirmed:
                refs += [c.ref() for c in check[:1]]
            out.append({"field": field, "value": g[0].value, "confidence": conf, "sources": refs,
                        "note": "; ".join(dict.fromkeys(f.note for f in g if f.note))})
    return out, sorted(set(invite))


def would(listing: dict, p: dict) -> str:
    """What the site will do with a proposal (owner's rule, 21 Oct 2026), for the dry run: 'applied' (empty field),
    and also for a second phone, email or website (kept beside the existing one), 'queued' or 'skipped' (already held)."""
    field, now = p["field"], (listing.get("now") or {}).get(p["field"])
    if now not in (None, "", []) and same_value(field, now, p["value"]) or (
            field in ("phone", "whatsapp", "emergency") and now and any(same_value("phone", n.strip(), p["value"]) for n in str(now).split(","))):
        return "skipped"
    if listing.get("protected") or p["confidence"] == "low":
        return "queued"
    if field in ("phone", "whatsapp", "emergency"):
        return "applied"  # a second number is kept beside the first, both marked unverified
    if now in (None, "", []):
        return "applied"
    return "applied" if field in ("email", "website") else "queued"
