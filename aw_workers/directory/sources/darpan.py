"""NGO Darpan (NITI Aayog). Its search and detail pages ask for a CAPTCHA (checked 18 Oct 2026), which the worker does not
solve or get around, so nothing is read from it automatically. The module stays registered: a listing whose Darpan ID is
already known is reported, and if Darpan opens an API or data download it is read here."""

from __future__ import annotations

KEY, NAME, HOST = "darpan", "NGO Darpan", "ngodarpan.gov.in"


def run(ctx, env, http) -> None:
    ctx.status[KEY] = "skipped: Darpan search needs a CAPTCHA"
