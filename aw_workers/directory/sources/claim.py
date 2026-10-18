"""What the organisation itself sent (approved claim or update form): the highest-trust source."""

from __future__ import annotations

from ..model import Fact

KEY, NAME, HOST = "claim", "The organisation (claim)", ""

MAP = {"aw_phone": "phone", "aw_whatsapp": "whatsapp", "aw_email": "email", "aw_website": "website", "aw_area": "", "aw_services": "services"}


def run(ctx, env, http) -> None:
    claim = ctx.listing.get("claim") or {}
    n = 0
    for k, v in claim.items():
        field = MAP.get(k)
        if field and v:
            ctx.add(Fact(field, [s.strip() for s in str(v).split(",") if s.strip()] if field == "services" else v, KEY, NAME))
            n += 1
    if claim.get("aw_website"):
        ctx.leads.setdefault("website", claim["aw_website"])
    ctx.status[KEY] = "ok" if n else "none"
