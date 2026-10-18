"""The AWBI list's address, already on the listing (aw_awbi_address): the full address and its PIN code."""

from __future__ import annotations

import re

from ..model import Fact, pincode_in

KEY, NAME, HOST = "awbi", "AWBI list of recognised organisations", "awbi.gov.in"
URL = "https://awbi.gov.in/"


def run(ctx, env, http) -> None:
    addr = re.sub(r"\s+", " ", (ctx.listing.get("awbi_address") or "")).strip(" ,.;–-")
    if not addr:
        ctx.status[KEY] = "none"
        return
    addr = re.sub(r"\s*[–-]\s*(\d{6})", r" \1", addr)
    pin = pincode_in(addr)
    ctx.add(Fact("address", addr, KEY, NAME, URL))
    if pin:
        ctx.add(Fact("pincode", pin, KEY, NAME, URL))
    ctx.status[KEY] = "ok"
