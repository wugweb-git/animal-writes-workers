"""Which worker is running, so the site's GET aw/v1/ai-keys returns only the keys attached to this worker (plus the
keys for all workers). A worker's id is its GitHub workflow file name without .yml (sourcing, summary-rewrite ...).

AW_WORKER_ID wins; otherwise the id comes from GITHUB_WORKFLOW_REF (owner/repo/.github/workflows/<file>.yml@ref).
With neither, no id is sent and the site answers as before (every key)."""

from __future__ import annotations

import os
import re


def worker_id(env=os.environ) -> str:
    """This worker's id, or '' when it can't be told."""
    explicit = (env.get("AW_WORKER_ID") or "").strip()
    if explicit:
        return explicit
    ref = (env.get("GITHUB_WORKFLOW_REF") or "").split("@", 1)[0]
    m = re.search(r"\.github/workflows/([^/]+)\.ya?ml$", ref)
    return m.group(1) if m else ""


def ai_keys_params(env=os.environ) -> dict:
    """Query parameters for aw/v1/ai-keys: {'worker': id}, or {} when the id is unknown."""
    wid = worker_id(env)
    return {"worker": wid} if wid else {}
