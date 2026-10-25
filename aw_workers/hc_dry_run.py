"""High Court judgments, dry run: what the judgments-hc source would keep, without sending anything or saving state.

    python -m aw_workers.hc_dry_run [--days 60] [--max-pdfs 1500] [--newest-first] [--json out.json]

Reads the public dataset only (s3://indian-high-court-judgments, no credentials) and prints each judgment it would keep:
title, court, decision date, why (statute cited, animal terms, or party) and the PDF. Nothing is posted to the site and
.state/documents.json is not written. --newest-first reads the newest decisions first (a live run reads oldest first and
carries on from there next run).
"""

from __future__ import annotations

import argparse
import json
import logging
import time

from . import documents
from .sources import Source


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--days", type=int, default=documents.HC_LOOKBACK, help="lookback in days (default 60)")
    ap.add_argument("--max-pdfs", type=int, default=documents.HC_MAX_PDFS, help="PDFs to read at most")
    ap.add_argument("--newest-first", action="store_true", help="read the newest decisions first")
    ap.add_argument("--json", help="also write the report to this file")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("pypdf").setLevel(logging.ERROR)

    documents._state = {}  # in memory only: save_state() is never called here
    src = Source(id="judgments-high-courts", name="High Court judgments (open dataset, CC BY 4.0)", kind="judgments-hc",
                 type="legal", url=documents.HC_BUCKET + "/", lookback_days=args.days)
    report: dict = {}
    t = time.monotonic()
    items = documents.parse_judgments_hc(src, max_pdfs=args.max_pdfs, report=report, newest_first=args.newest_first)
    report["seconds"] = round(time.monotonic() - t)
    kept = report.get("kept", [])
    print(f"rows in changed files: {report.get('rows', 0)}; skipped by metadata: {report.get('skipped', {})}; "
          f"candidates: {report.get('candidates', 0)}; PDFs read: {report.get('read', 0)} (not in the dataset {report.get('missing', 0)}, download failed {report.get('failed', 0)}, "
          f"no text {report.get('no text', 0)}){' (stopped at the time limit)' if report.get('stopped') else ''}; "
          f"kept: {len(kept)}; {report['seconds']} s")
    for k in sorted(kept, key=lambda k: k["date"], reverse=True):
        print(f"- {k['date']} | {k['court']} | {k['title']} | {k['why']} | {k['pdf']}")
    for e in report.get("errors", []):
        print(f"error: {e}")
    if args.json:
        with open(args.json, "w") as fh:
            json.dump({**report, "items": [i.title for i in items]}, fh, ensure_ascii=False, indent=1, default=str)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
