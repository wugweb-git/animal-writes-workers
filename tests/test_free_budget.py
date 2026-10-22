import datetime as dt

from aw_workers import free_budget, summarise


def test_share_spreads_what_is_left_over_the_days_remaining_runs(monkeypatch):
    monkeypatch.delenv("AW_FREE_RUN_EVERY_H", raising=False)
    morning = dt.datetime(2026, 10, 22, 0, 30, tzinfo=dt.timezone.utc)  # 8 runs to go
    evening = dt.datetime(2026, 10, 22, 22, 0, tzinfo=dt.timezone.utc)  # the last one
    assert free_budget.share(50, morning) == 7 and free_budget.share(50, evening) == 50 and free_budget.share(0, morning) == 0


class _Resp:
    def __init__(self, data):
        self._d = data

    def json(self):
        return {"data": self._d}


class _Session:
    def __init__(self, accounts):
        self.accounts = accounts

    def get(self, url, headers, timeout):
        key = headers["Authorization"].split()[-1]
        return _Resp({"creator_user_id": self.accounts[key], "free_model_daily_requests": {"used": 40, "limit": 50, "remaining": 10}})


def test_keys_of_one_account_share_one_cap(monkeypatch):
    monkeypatch.setattr(free_budget, "_accounts", {})
    monkeypatch.setattr(free_budget, "_by_key", {})
    monkeypatch.setattr(free_budget, "runs_left", lambda now=None: 5)  # 10 left / 5 runs = 2 for this run
    s = _Session({"k1": "user_A", "k2": "user_A", "k3": "user_B"})
    a1, a2, b = (free_budget.openrouter_account(k, session=s) for k in ("k1", "k2", "k3"))
    assert a1 is a2 and a1 is not b
    assert [a1.take(), a2.take(), a1.take()] == [True, True, False] and b.take()


def test_capped_free_model_is_skipped_without_a_call(monkeypatch):
    acct = free_budget.Account("user_A", 0, 50, 0)
    monkeypatch.setattr(free_budget, "openrouter_account", lambda key, session=None: acct)

    class NoCalls:
        def post(self, *a, **kw):
            raise AssertionError("no call expected")

    rows = [{"provider": "openrouter", "model": "openai/gpt-4o-mini", "url": "https://openrouter.ai/api/v1", "key": "k1"}]
    free = next(b for b in summarise.backends_from_rows(rows) if b.model.endswith(":free"))
    free.session = NoCalls()
    try:
        free.ask("hi")
    except summarise.FreeQuotaUsed as exc:
        assert exc.not_called
    else:
        raise AssertionError("expected FreeQuotaUsed")
