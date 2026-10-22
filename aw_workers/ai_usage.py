"""How much work each AI key did in a run (owner, 20 Oct 2026: Settings → AI models shows it, above all for keys in
cooldown). Every provider call goes through a backend's ask(); summarise.py wraps those and counts here per key and
model: calls, answers, rate-limited and failed. When the run ends the counts go to the site (POST aw/v1/ai-usage),
keyed by the same id the site uses for a key (sha256, first 16 hex characters) and its last 4 characters, never the
key itself. A run without the site's login (local tests) sends nothing."""

from __future__ import annotations

import atexit
import hashlib
import logging
import os

log = logging.getLogger(__name__)

USAGE: dict[str, dict] = {}
_state = {"registered": False}


def key_id(key: str) -> str:
    return hashlib.sha256((key or "").encode()).hexdigest()[:16]


def _outcome(exc: BaseException | None) -> str:
    if exc is None:
        return "ok"
    text = f"{type(exc).__name__} {exc}".lower()
    if "429" in text or "too many requests" in text or "rate" in text and "limit" in text:
        return "limited"
    return "failed"


def record(backend, exc: BaseException | None) -> None:
    if not _state["registered"]:
        _state["registered"] = True
        atexit.register(report)
    if getattr(exc, "not_called", False):  # skipped before any call (a free-model cap): nothing to count
        return
    key = str(getattr(backend, "api_key", "") or "")
    if not key:
        return
    row = USAGE.setdefault(key_id(key), {"provider": getattr(backend, "name", ""), "last4": key[-4:], "calls": 0,
                                         "ok": 0, "limited": 0, "failed": 0, "models": {}})
    out = _outcome(exc)
    row["calls"] += 1
    row[out] += 1
    m = row["models"].setdefault(str(getattr(backend, "model", "") or ""), {"calls": 0, "ok": 0, "limited": 0, "failed": 0})
    m["calls"] += 1
    m[out] += 1
    if not _state["registered"]:
        _state["registered"] = True
        atexit.register(report)


def counted(ask):
    """Wrap a backend's ask() so every call is counted."""

    def wrapper(self, *args, **kw):
        try:
            reply = ask(self, *args, **kw)
        except BaseException as exc:
            record(self, exc)
            raise
        record(self, None)
        return reply

    wrapper.__wrapped__ = ask
    return wrapper


def report(env=None, session=None) -> bool:
    """Send this run's counts to the site. True when sent."""
    env = env if env is not None else os.environ
    base, user, pw = env.get("WP_BASE_URL"), env.get("WP_INGEST_USER"), env.get("WP_INGEST_APP_PASSWORD")
    from . import free_budget

    accounts = free_budget.summary()
    if not (USAGE or accounts) or not (base and user and pw):
        return False
    run = env.get("GITHUB_RUN_ID", "")
    body = {
        "workflow": env.get("GITHUB_WORKFLOW", "") or "local",
        "link": f"{env.get('GITHUB_SERVER_URL', 'https://github.com')}/{env.get('GITHUB_REPOSITORY', '')}/actions/runs/{run}" if run else "",
        "dry_run": str(env.get("DRY_RUN", "")).lower() in ("1", "true", "yes"),
        "keys": [{"id": k, **v} for k, v in USAGE.items()],
        "accounts": accounts,  # free-model daily caps per account (free_budget.py)
    }
    try:
        import requests

        resp = (session or requests).post(base.rstrip("/") + "/wp-json/aw/v1/ai-usage", json=body, auth=(user, pw), timeout=30)
        resp.raise_for_status()
        USAGE.clear()
        return True
    except Exception as exc:  # the report must never fail a run
        log.warning("AI usage not sent: %s", exc)
        return False
