"""Copy the site's lists (Settings → Sourcing) into sources.yaml (owner, 20 Oct 2026), so the file the workers fall
back on when the site cannot be reached is never older than the site. Only while "Workers use these lists" is on; the
policy then carries the lists exactly as the workers read them.

From the sourcing restructure's phase 3 (owner, 25 Oct 2026) it also copies the summary rules (GET summary-rules) into
aw_workers/summary_rules.json and the Directory schema with its field rules and trust order (GET enrich/config) into
aw_workers/directory/directory_schema.json. Those two are the site's own rules, so they are copied whatever the lists'
switch says; a site from before phase 3 sends no schema, and directory_schema.json is then left as it is.

Writes a file only when it changed; prints "changed" or "unchanged" (last line)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from aw_workers import counts, ingest, sources, summary_rules  # noqa: E402

HEADER = ("# Sources for the Animal Writes sourcing workers. COPIED FROM THE SITE (Settings → Sourcing) by\n"
          "# .github/workflows/sources-sync.yml while \"Workers use these lists\" is on: edit the lists on the site, not here.\n"
          "# The workers read this file only when the site cannot be reached.\n")
ROOT = Path(__file__).resolve().parent.parent
SCHEMA_KEYS = ("schema", "priority", "field_rules")


def sync_lists(raw: dict) -> bool | None:
    """sources.yaml from the policy: True changed, False unchanged, None when the lists' switch is off."""
    # The new blocks (sources_v2, category_names, match, schema, sections_v2) may come beside `config`: keep them too.
    config = sources.with_v2_blocks(raw["config"], raw) if isinstance(raw.get("config"), dict) else raw.get("config")
    if not isinstance(config, dict) or not config.get("sources"):
        return None
    path = Path(os.environ.get("AW_SOURCES", "sources.yaml"))
    old = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else None
    if old == config:
        return False
    path.write_text(HEADER + yaml.safe_dump(config, sort_keys=False, allow_unicode=True, width=120), encoding="utf-8")
    return True


def write_json(path: Path, data: dict) -> bool:
    """Write `data` as JSON when it differs from what the file holds; True when written."""
    old = None
    if path.exists():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            old = None
    if old == data:
        return False
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return True


def sync_summary_rules(client) -> bool | None:
    """summary_rules.json from the site's rules (read as the workers read them); None when the site sent none."""
    site = client.summary_rules()
    if not isinstance(site, dict) or not isinstance(site.get("sections"), dict) or not site["sections"]:
        return None
    path = Path(os.environ.get("AW_SUMMARY_RULES", str(ROOT / "aw_workers" / "summary_rules.json")))
    return write_json(path, summary_rules.for_file(site))


def sync_directory_schema(client) -> bool | None:
    """directory_schema.json from GET enrich/config (schema, priority, field_rules); None for a site before phase 3."""
    cfg = client._call("GET", "/enrich/config", timeout=60)
    if not isinstance(cfg, dict) or not isinstance(cfg.get("field_rules"), dict):
        return None
    path = Path(os.environ.get("AW_DIRECTORY_SCHEMA", str(ROOT / "aw_workers" / "directory" / "directory_schema.json")))
    return write_json(path, {k: cfg.get(k) for k in SCHEMA_KEYS})


def main() -> int:
    client = ingest.Client(os.environ["WP_BASE_URL"], os.environ["WP_INGEST_USER"], os.environ["WP_INGEST_APP_PASSWORD"])
    lists = sync_lists(client.policy())
    if lists is None:
        print("sources.yaml: unchanged (Workers use these lists is off: sources.yaml stays as it is)")
    else:
        print("sources.yaml: " + ("changed" if lists else "unchanged"))
    done = {"lists": lists}
    for name, fn in (("summary_rules.json", sync_summary_rules), ("directory_schema.json", sync_directory_schema)):
        try:
            done[name] = fn(client)
        except ingest.AuthError:
            raise
        except Exception as exc:  # noqa: BLE001 - one file failing never stops the others
            print(f"{name}: not copied ({type(exc).__name__})")
            done[name] = None
            continue
        print(f"{name}: " + ("changed" if done[name] else "unchanged" if done[name] is False else "not sent by the site"))
    changed = sum(1 for v in done.values() if v)
    counts.write(changed=changed, lists="site" if lists is not None else "yaml")
    print("changed" if changed else "unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
