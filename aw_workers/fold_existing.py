"""Fold duplicates already on the site into their first story (owner, 15 Oct 2026; run once by hand).

AW_PAIRS="dup:keep,dup:keep" python -m aw_workers.fold_existing
Each duplicate's report is read again from its publisher's page (its own words); the facts it adds and its credit go
to the first story, and the duplicate goes private with a redirect to the first story. Nothing is deleted.
"""

from __future__ import annotations

import logging
import os
import sys

from . import ingest, merge, summarise

log = logging.getLogger("aw_workers.fold_existing")


def pairs(value: str) -> list[tuple[int, int]]:
    out = []
    for part in (value or "").replace(" ", "").split(","):
        if part:
            dup, keep = part.split(":")
            out.append((int(dup), int(keep)))
    return out


def main(env=os.environ) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    dry = env.get("DRY_RUN", "false").lower() == "true"
    client = ingest.Client(env["WP_BASE_URL"], env["WP_INGEST_USER"], env["WP_INGEST_APP_PASSWORD"])
    summariser = summarise.Summariser(summarise.backends_from_env(env, ledger={}))
    failed = 0
    for dup, keep in pairs(env.get("AW_PAIRS", "")):
        d = client.story(dup)
        text = merge.page_text(d.get("source_url") or "")
        where = "publisher page"
        if len(text) < 200:  # the page could not be read: credit only (our summary of it is not the report's own text)
            text, where = "", "nothing readable"
        body = merge.fold(summariser, client, keep, title=d.get("title") or "", url=d.get("source_url") or "",
                          publisher=d.get("publisher") or "", text=text, dup_id=dup, dry_run=dry)
        if body is None:
            failed += 1
        print(f"#{dup} -> #{keep}: text from {where}; " + ("FAILED" if body is None else
              ("added: " + body["added_text"] if body["added_text"] else "credit only")))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
