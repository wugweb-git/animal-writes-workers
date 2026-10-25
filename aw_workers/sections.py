"""Sections from the site (sourcing restructure phase 3, owner 25 Oct 2026).

A section is one of the site's content types (a registered public post type, or one that opts in, such as aw_press). The
site sends them in the sourcing policy as `sections_v2` (inside `config`, so Sources sync copies it into sources.yaml):

    [{"post_type": "aw_news", "label": "News", "worker_type": "news", "categories": [...], "tags": [...],
      "tag_rules": [...], "summary": "aw_news"}, ...]

`worker_type` is the item type the workers give an item of that section ("news", "legal", "research", ...); an empty
one means the workers never file items there. The code map below is the fallback for sites (or sources.yaml copies)
from before phase 3, and it is exactly the map the workers used until then.
"""

from __future__ import annotations

import logging

log = logging.getLogger("aw_workers.sections")

# The workers' item types and the site's post types, as before phase 3 (sources.py SITE_TYPES).
FALLBACK = {"news": "aw_news", "legal": "aw_legal", "opportunity": "aw_opportunity", "research": "post"}

_types: dict[str, str] = dict(FALLBACK)  # worker type => post type, from the site once it sends sections_v2
_v2: list[dict] = []


def valid(v2) -> list[dict] | None:
    """The `sections_v2` list when every entry names its post type; None otherwise (then the v1 `sections` are read)."""
    if not isinstance(v2, list) or not v2:
        return None
    if not all(isinstance(s, dict) and str(s.get("post_type") or "").strip() for s in v2):
        log.warning("sections_v2 has an entry without a post type: reading the v1 sections")
        return None
    return v2


def use(v2) -> bool:
    """Read the site's sections (True), or go back to the fallback map when it sent none (False)."""
    global _v2
    good = valid(v2)
    _types.clear()
    _types.update(FALLBACK)
    _v2 = []
    if good is None:
        return False
    for s in good:
        pt = str(s["post_type"])
        wt = str(s.get("worker_type") or "").strip()
        if wt:
            _types[wt] = pt
    _v2 = good
    return True


def post_type_for(kind: str) -> str:
    """The post type of a worker item type ("news" → "aw_news"); a post type is returned as it is."""
    kind = str(kind or "")
    return _types.get(kind, kind)


def allowed(v2) -> dict[str, dict]:
    """post type => {"categories": [...], "tags": [...]} from `sections_v2` (the v1 `sections` shape)."""
    out = {}
    for s in valid(v2) or []:
        out[str(s["post_type"])] = {"categories": list(s.get("categories") or []), "tags": list(s.get("tags") or [])}
    return out


def summary_type(post_type: str) -> str:
    """The post type whose summary rule a section follows (its own, unless the site says otherwise)."""
    for s in _v2:
        if s.get("post_type") == post_type:
            return str(s.get("summary") or post_type)
    return post_type
