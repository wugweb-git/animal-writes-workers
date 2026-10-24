"""This run's totals for the site's Workers tab (Settings → Sourcing → Workers, owner 24 Oct 2026).

Each worker calls write() where it already prints its totals; the workflow's last step (.github/actions/report) reads
the file and sends it with the end report. Outside GitHub Actions (no RUNNER_TEMP) nothing is written.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


def path() -> Path | None:
    tmp = os.environ.get("RUNNER_TEMP")
    return Path(tmp) / "aw-counts.json" if tmp else None


def write(**counts) -> None:
    """Merge numbers (or short strings) into this run's counts. Never fails the run."""
    p = path()
    if p is None:
        return
    try:
        old = json.loads(p.read_text()) if p.exists() else {}
        if not isinstance(old, dict):
            old = {}
        old.update({k: v for k, v in counts.items() if isinstance(v, (int, float, str, bool))})
        p.write_text(json.dumps(old))
    except Exception:  # the counts must never fail a run
        pass
