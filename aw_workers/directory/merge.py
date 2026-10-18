"""From facts to proposals, with confidence (owner's brief):
high   = two or more sources agree, or the organisation's claim, or a Google match confirming a stored value;
medium = one source (not Google);
low    = sources disagree.
Google values are never proposed (except the place ID and Maps link); a field only Google has is "Invite to claim".
Values read by the AI are at most medium."""

from __future__ import annotations

from .model import CHECK, STORE, same_value

MULTI = {"phone": 2, "whatsapp": 1, "emergency": 1}  # how many different numbers may be proposed
AI_FIELDS = {"services", "animals", "areas_served", "description", "reg_type", "head"}
PRIORITY = ["claim", "website", "darpan", "wikidata", "osm", "social", "awbi", "google_places"]
INVITE_FIELDS = ("phone", "hours", "address", "website")


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


def proposals(ctx) -> tuple[list[dict], list[str]]:
    """[{field, value, confidence, sources, note}], and the fields only Google has (Invite to claim)."""
    by: dict[str, dict[str, list]] = {}
    for f in ctx.facts:
        by.setdefault(f.field, {}).setdefault(f.role, []).append(f)
    out, invite = [], []
    for field, d in by.items():
        store, check = d.get(STORE, []), d.get(CHECK, [])
        if not store:
            if check and field in INVITE_FIELDS and not (ctx.listing.get("now") or {}).get(field):
                invite.append(field)  # only Google has it
            continue
        groups = _groups(store, field)
        groups.sort(key=lambda g: (-any(f.source_key == "claim" for f in g), -len({f.source_key for f in g}),
                                   min(PRIORITY.index(f.source_key) if f.source_key in PRIORITY else 99 for f in g)))
        keep = groups[: MULTI.get(field, 1)]
        rivals = groups[len(keep):]
        for g in keep:
            keys = {f.source_key for f in g}
            confirmed = any(same_value(field, c.value, g[0].value) for c in check)
            if "claim" in keys:
                conf = "high"
            elif field in ("place_id", "maps_url"):
                conf = "high" if getattr(ctx, "google_match", 0) >= 0.85 else "medium"
            elif len(keys) >= 2 or confirmed:
                conf = "high"
            elif field not in MULTI and any({f.source_key for f in x} - keys for x in rivals):
                conf = "low"  # another source says something else
            else:
                conf = "medium"
            if field in AI_FIELDS and "claim" not in keys and conf == "high":
                conf = "medium"
            refs = list({(f.source_key, f.source_url): f.ref() for f in g}.values())
            if confirmed:
                refs += [c.ref() for c in check[:1]]
            out.append({"field": field, "value": g[0].value, "confidence": conf, "sources": refs,
                        "note": "; ".join(dict.fromkeys(f.note for f in g if f.note))})
    return out, sorted(set(invite))


def would(listing: dict, p: dict) -> str:
    """What the site would do with a proposal (for the dry run): applied, queued or skipped (already held)."""
    now = (listing.get("now") or {}).get(p["field"])
    if now not in (None, "", []) and same_value(p["field"], now, p["value"]) or (
            p["field"] in ("phone", "whatsapp", "emergency") and now and any(same_value("phone", n.strip(), p["value"]) for n in str(now).split(","))):
        return "skipped"
    empty = now in (None, "", []) or p["field"] in ("whatsapp", "emergency")
    if listing.get("protected") or p["confidence"] != "high" or not empty:
        return "queued"
    return "applied"
