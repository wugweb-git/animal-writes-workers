"""The summary rewrite always stops before the job timeout (25 Oct 2026: runs were cancelled at 50 minutes while one
item walked ~150 key/model pairs; the stop was only checked between items)."""

import json

import pytest
import requests

from aw_workers import summarise, summary_rewrite as sr


class Clock:
    def __init__(self, t: float = 0.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


class SlowBackend:
    """Never answers: each call runs into its timeout (the clock moves on by that much)."""

    name = "slow"
    model = "m"
    min_interval = 0.0

    def __init__(self, clock: Clock, timeout: float = 60.0):
        self.clock, self.timeout, self.calls = clock, timeout, []

    def ask(self, prompt: str) -> str:
        self.calls.append(self.timeout)
        self.clock.t += self.timeout
        raise requests.Timeout("Read timed out.")


def test_deadline_counts_from_the_start_less_the_reserve():
    clock = Clock(100.0)
    d = sr.Deadline(40, start=0.0, clock=clock, reserve=90)
    assert d.left() == pytest.approx(40 * 60 - 90 - 100)
    assert not d.expired(sr.ITEM_MIN_S)
    clock.t = d.end - sr.ITEM_MIN_S + 1
    assert d.expired(sr.ITEM_MIN_S)


def test_default_start_is_the_process_start():
    d = sr.Deadline(40, reserve=0)
    assert d.end == pytest.approx(sr._STARTED + 2400)


def test_call_timeout_is_cut_to_the_time_left():
    clock = Clock(0.0)
    d = sr.Deadline(1, start=0.0, clock=clock, reserve=0)  # 60 s
    assert d.call_timeout(30) == 30  # plenty left: the provider's own limit
    clock.t = 20.0  # 40 s left
    assert d.call_timeout(30, overhead=10, attempts=2) == pytest.approx(15)  # 2 attempts + waits fit in 40 s
    clock.t = 55.0
    with pytest.raises(sr.TimeUp):
        d.call_timeout(30)


def test_each_ask_gets_a_timeout_that_ends_before_the_stop():
    clock = Clock(0.0)
    d = sr.Deadline(1, start=0.0, clock=clock, reserve=0)
    b = SlowBackend(clock, timeout=60)
    rw = sr.Rewriter(summarise.Summariser([b]), deadline=d)
    clock.t = 10.0  # 50 s left: (50 - MAX_RETRY_WAIT) / 2 attempts
    with pytest.raises(requests.Timeout):
        rw._call(b, "p")
    assert b.calls == [pytest.approx((50 - summarise.MAX_RETRY_WAIT) / 2)]
    assert b.__dict__["_aw_cap_s"] == 60  # the provider's own limit is kept for later calls


def test_a_hanging_item_stops_at_the_deadline_not_the_job_timeout():
    clock = Clock(0.0)
    d = sr.Deadline(10, start=0.0, clock=clock, reserve=0)  # 600 s
    backends = [SlowBackend(clock, timeout=60) for _ in range(150)]  # the ~150 key/model pairs of 24 Oct
    rw = sr.Rewriter(summarise.Summariser(backends), deadline=d, max_tries=10_000)
    with pytest.raises(sr.TimeUp):
        rw._ask("prompt", lambda c: ([], []))
    assert clock.t <= d.end  # no call ran past the stop
    assert sum(len(b.calls) for b in backends) < 150


def test_tries_per_ask_are_capped():
    clock = Clock(0.0)
    backends = [SlowBackend(clock, timeout=1) for _ in range(100)]
    rw = sr.Rewriter(summarise.Summariser(backends), deadline=sr.Deadline(600, start=0.0, clock=clock), max_tries=30)
    assert rw._ask("prompt", lambda c: ([], [])) is None
    assert sum(len(b.calls) for b in backends) == 30


def test_no_cooldown_wait_past_the_stop(monkeypatch):
    clock = Clock(0.0)
    d = sr.Deadline(1, start=0.0, clock=clock, reserve=0)
    s = summarise.Summariser([SlowBackend(clock)])
    s.cool = {0: sr.time.monotonic() + 3600}  # the key rests an hour: far past the stop
    rw = sr.Rewriter(s, deadline=d)
    monkeypatch.setattr(sr, "wait_for_cooldown", lambda owner: pytest.fail("waited past the stop"))
    assert rw._wait_for_cooldown() is False


class FakeClient:
    def __init__(self, rows):
        self.rows, self.posts = rows, []

    def _call(self, method, path, **kw):
        if method == "GET":
            if (kw.get("params") or {}).get("progress"):
                return {"counts": {"news": {"open": 1}}}
            return {"items": self.rows, "next": None}
        self.posts.append(kw.get("json"))
        return {}


def rows(n):
    return [{"id": i, "type": "aw_news", "section": "news", "summary": "Too short.", "md5": "x",
             "source_url": f"https://example.org/{i}", "title": f"Story {i}", "tries": 0} for i in range(1, n + 1)]


def run(tmp_path, monkeypatch, process_, clock, n=10, minutes=40):
    monkeypatch.setattr(sr.counts, "write", lambda **kw: None)
    chain = tmp_path / "chain.txt"
    client = FakeClient(rows(n))
    env = {"BATCH": str(n), "PAUSE_S": "4", "AW_CHAIN_FILE": str(chain)}
    d = sr.Deadline(minutes, start=0.0, clock=clock)
    code = sr.main(env, client=client, backends=[SlowBackend(clock)], deadline=d, process_=process_,
                   sleep=lambda s: setattr(clock, "t", clock.t + s))
    return code, client, chain.read_text(), d


def stats_line(out):
    return json.loads(next(line for line in out.splitlines() if line.startswith("RUN "))[4:])


def test_run_stops_before_the_next_item_and_reports_success(tmp_path, monkeypatch, capsys):
    clock = Clock(0.0)

    def process_(rw, row):  # each item takes 10 minutes, cut at the stop (its calls' timeouts are)
        clock.t += min(600, rw.deadline.left())
        return {"action": "settle", "reason": "kept"}

    code, client, chain, d = run(tmp_path, monkeypatch, process_, clock)
    stats = stats_line(capsys.readouterr().out)
    assert code == 0
    assert stats["settled"] == 4 and stats["left"] == 6  # the 5th would start with under ITEM_MIN_S left
    assert clock.t <= d.end  # the reserve is untouched for the write, progress read and report
    assert chain == "4"


def test_time_up_mid_item_leaves_it_open(tmp_path, monkeypatch, capsys):
    clock = Clock(0.0)

    def process_(rw, row):
        if row["id"] == 2:
            raise sr.TimeUp("0 s left")
        clock.t += 60
        return {"action": "settle", "reason": "kept"}

    code, client, chain, _ = run(tmp_path, monkeypatch, process_, clock, n=5)
    stats = stats_line(capsys.readouterr().out)
    assert code == 0
    assert stats["settled"] == 1 and stats["left"] == 4 and stats["failed"] == 0 and stats["error"] == 0
    assert not any(p.get("id") == 2 for p in client.posts if isinstance(p, dict))  # never marked failed
    assert chain == "1"


def test_stopped_before_any_item_still_chains_the_next_run(tmp_path, monkeypatch, capsys):
    clock = Clock(40 * 60)  # setup ate the whole budget

    code, client, chain, _ = run(tmp_path, monkeypatch, lambda rw, row: pytest.fail("item started"), clock, n=3)
    stats = stats_line(capsys.readouterr().out)
    assert code == 0 and stats["left"] == 3
    assert chain == "1"  # items remain: the workflow starts the next run
