"""Copy the site's lists (Settings → Sourcing) into sources.yaml (owner, 20 Oct 2026), so the file the workers fall
back on when the site cannot be reached is never older than the site. Only while "Workers use these lists" is on; the
policy then carries the lists exactly as the workers read them. Writes the file only when the lists changed; prints
"changed" or "unchanged"."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from aw_workers import ingest  # noqa: E402

HEADER = ("# Sources for the Animal Writes sourcing workers. COPIED FROM THE SITE (Settings → Sourcing) by\n"
          "# .github/workflows/sources-sync.yml while \"Workers use these lists\" is on: edit the lists on the site, not here.\n"
          "# The workers read this file only when the site cannot be reached.\n")


def main() -> int:
    client = ingest.Client(os.environ["WP_BASE_URL"], os.environ["WP_INGEST_USER"], os.environ["WP_INGEST_APP_PASSWORD"])
    config = client.policy().get("config")
    if not isinstance(config, dict) or not config.get("sources"):
        print("unchanged (Workers use these lists is off: sources.yaml stays as it is)")
        return 0
    path = Path(os.environ.get("AW_SOURCES", "sources.yaml"))
    old = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else None
    if old == config:
        print("unchanged")
        return 0
    path.write_text(HEADER + yaml.safe_dump(config, sort_keys=False, allow_unicode=True, width=120), encoding="utf-8")
    print("changed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
