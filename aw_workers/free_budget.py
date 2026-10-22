"""Daily caps for free models, grouped by account (owner, 22 Oct 2026: "add caps so we don't lose the free tokens").

OpenRouter gives each ACCOUNT 50 free-model requests a day (1,000 with credit), however many keys it has. Every key
says which account it belongs to and how many free requests that account has left today (GET /api/v1/key), so the
keys of one account share one counter here. A run takes only its share of what is left: remaining / the runs still to
come before the daily reset at 00:00 UTC (one about every AW_FREE_RUN_EVERY_H hours, default 3). The first run of
the day no longer uses up the whole day, and a run never calls past the account's cap.

When the share is used, the key's free models raise FreeQuotaUsed before any call (not counted as a call), and the
summariser drops them for the run like a real "free-models-per-day" answer."""

from __future__ import annotations

import datetime as dt
import logging
import math
import os

log = logging.getLogger(__name__)

KEY_URL = "https://openrouter.ai/api/v1/key"


class Account:
    def __init__(self, account_id: str, remaining: int, limit: int, share: int):
        self.id, self.remaining, self.limit, self.left = account_id, remaining, limit, share

    def take(self) -> bool:
        if self.left <= 0:
            return False
        self.left -= 1
        return True


_accounts: dict[str, Account] = {}  # account id -> Account
_by_key: dict[str, Account | None] = {}  # key -> its account (None: unknown, no cap)


def runs_left(now: dt.datetime | None = None) -> int:
    now = now or dt.datetime.now(dt.timezone.utc)
    every = float(os.environ.get("AW_FREE_RUN_EVERY_H") or 3)
    hours = 24 - (now.hour + now.minute / 60)
    return max(1, math.ceil(hours / every))


def share(remaining: int, now: dt.datetime | None = None) -> int:
    return max(0, math.ceil(max(0, remaining) / runs_left(now)))


def openrouter_account(key: str, session=None) -> Account | None:
    """The key's account with this run's share of its free requests; None when OpenRouter does not say (no cap)."""
    if key in _by_key:
        return _by_key[key]
    acct = None
    try:
        import requests

        resp = (session or requests).get(KEY_URL, headers={"Authorization": f"Bearer {key}"}, timeout=20)
        data = (resp.json() or {}).get("data") or {}
        free = data.get("free_model_daily_requests") or {}
        account_id = str(data.get("creator_user_id") or data.get("organization_id") or "")
        if account_id and "remaining" in free:
            acct = _accounts.get(account_id)
            if acct is None:
                remaining, limit = int(free.get("remaining") or 0), int(free.get("limit") or 0)
                acct = _accounts[account_id] = Account(account_id, remaining, limit, share(remaining))
                log.info("free models: OpenRouter account …%s has %d of %d left today; this run uses up to %d",
                         account_id[-4:], remaining, limit, acct.left)
    except Exception as exc:  # noqa: BLE001
        log.info("free models: OpenRouter account not read: %s", type(exc).__name__)
    _by_key[key] = acct
    return acct
