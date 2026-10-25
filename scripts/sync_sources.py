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
from aw_workers import counts, ingest, sources  # noqa: E402

HEADER = ("# Sources for the Animal Writes sourcing workers. COPIED FROM THE SITE (Settings → Sourcing) by\n"
          "# .github/workflows/sources-sync.yml while \"Workers use these lists\" is on: edit the lists on the site, not here.\n"
          "# The workers read this file only when the site cannot be reached.\n")


def main() -> int:
    client = ingest.Client(os.environ["WP_BASE_URL"], os.environ["WP_INGEST_USER"], os.environ["WP_INGEST_APP_PASSWORD"])
    raw = client.policy()
    # The new blocks (sources_v2, category_names, match, schema, and from phase 2 the keyword table as keywords_v2) may
    # come beside `config`: keep them in the copy too.
    config = sources.with_v2_blocks(raw["config"], raw) if isinstance(raw.get("config"), dict) else raw.get("config")
    if not isinstance(config, dict) or not config.get("sources"):
        print("unchanged (Workers use these lists is off: sources.yaml stays as it is)")
        counts.write(changed=0, lists="yaml")
        return 0
    path = Path(os.environ.get("AW_SOURCES", "sources.yaml"))
    old = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else None
    if old == config:
        print("unchanged")
        counts.write(changed=0)
        return 0
    path.write_text(HEADER + yaml.safe_dump(config, sort_keys=False, allow_unicode=True, width=120), encoding="utf-8")
    print("changed")
    counts.write(changed=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
