"""Summariser follow-ups (owner, 25 Oct 2026): short items get another try (rules change, a source that now says
more), and refused or thin sources are read again through Firecrawl within the free credits."""

import json
from datetime import datetime, timedelta, timezone

from aw_workers import firecrawl, summary_rewrite as sr

LONG = "The court directed the municipal body to act within thirty days on the feeders' complaint. " * 40


# ---- Firecrawl when the plain read gives little ----


def _plain(page):
    return lambda url, session=None: dict(page)


def test_refused_page_is_read_through_firecrawl(monkeypatch):
    monkeypatch.setattr(sr, "_plain_read", _plain({"text": "", "published": ["2026-10-20"], "status": 403}))
    monkeypatch.setattr(sr.firecrawl, "page_text", lambda url, limit=4000: LONG)
    page = sr.read_source("https://www.petaindia.com/blog/x/")
    assert page["text"] == LONG and page["status"] == "firecrawl (403)" and page["published"] == ["2026-10-20"]


def test_thin_page_and_textless_pdf_use_firecrawl_only_when_it_gives_more(monkeypatch):
    calls = []
    monkeypatch.setattr(sr.firecrawl, "page_text", lambda url, limit=4000: calls.append(url) or "short")
    monkeypatch.setattr(sr, "_plain_read", _plain({"text": "A thin page of a few words.", "published": [], "status": 200}))
    assert sr.read_source("https://x.test/a")["text"] == "A thin page of a few words."  # Firecrawl gave less
    monkeypatch.setattr(sr, "_plain_read", _plain({"text": "", "published": [], "status": "pdf without text"}))
    monkeypatch.setattr(sr.firecrawl, "page_text", lambda url, limit=4000: calls.append(url) or LONG)
    assert sr.read_source("https://x.test/b.pdf")["status"] == "firecrawl (pdf without text)"
    assert len(calls) == 2


def test_a_full_page_never_spends_a_credit(monkeypatch):
    monkeypatch.setattr(sr, "_plain_read", _plain({"text": LONG, "published": [], "status": 200}))
    monkeypatch.setattr(sr.firecrawl, "page_text", lambda *a, **k: (_ for _ in ()).throw(AssertionError("called")))
    assert sr.read_source("https://x.test/c")["status"] == 200


def test_run_share_keeps_sourcing_its_credits():
    now = datetime(2026, 10, 25, tzinfo=timezone.utc)
    end = now + timedelta(days=28)
    assert firecrawl.run_share(None, 20, 24, 20, now) == 0  # unknown: none
    assert firecrawl.run_share({"remaining": 500, "period_end": end}, 20, 24, 20, now) == 0  # all kept for sourcing
    assert firecrawl.run_share({"remaining": 961, "period_end": end}, 20, 24, 20, now) == 1  # a little spare: 1 a run
    assert firecrawl.run_share({"remaining": 100000, "period_end": end}, 20, 24, 20, now) == 20  # never above the cap


def test_run_cap_limits_page_reads(monkeypatch):
    class Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"data": {"markdown": LONG}}

    class S:
        calls = 0

        def post(self, *a, **k):
            S.calls += 1
            return Resp()

    monkeypatch.setattr(firecrawl, "_state", {"key": "fc-x", "used": 0, "stopped": False})
    firecrawl.set_run_cap(1)
    assert firecrawl.page_text("https://x.test/1", session=S()) and firecrawl.page_text("https://x.test/2", session=S()) == ""
    assert S.calls == 1


# ---- short items tried again ----


class Client:
    def __init__(self, items, shorts, site_has_short=True):
        self.items, self.shorts, self.site_has_short, self.posts, self.gets = items, shorts, site_has_short, [], []

    def _call(self, method, path, **kw):
        params = kw.get("params") or {}
        if method == "GET":
            self.gets.append(params)
            if params.get("progress"):
                return {"counts": {}}
            if params.get("short"):
                if not self.site_has_short:
                    return {"items": self.items, "next": None}  # an older site ignores ?short=1
                return {"items": self.shorts[: params["per_page"]], "due": len(self.shorts)}
            return {"items": self.items, "next": None}
        self.posts.append(kw.get("json"))
        return {}


def row(i, **kw):
    return {"id": i, "type": "aw_news", "section": "news", "summary": "Too short.", "md5": "m", "title": f"Story {i}",
            "source_url": f"https://x.test/{i}", "tries": 0, **kw}


class NoBackend:
    name, model, min_interval = "none", "m", 0.0

    def ask(self, prompt):
        raise AssertionError("no AI call expected")


def run(monkeypatch, client, process_, env=None, pages=None):
    monkeypatch.setattr(sr.counts, "write", lambda **kw: None)
    monkeypatch.setattr(sr, "read_source", lambda url, session=None: (pages or {}).get(url, {"text": "", "status": 200}))
    sr.main({"BATCH": "5", "PAUSE_S": "0", **(env or {})}, client=client, backends=[NoBackend()], process_=process_,
            sleep=lambda s: None)


def test_short_items_come_after_new_ones_and_only_with_room(monkeypatch):
    seen = []
    client = Client([row(1), row(2)], [row(9, why="rules", src_chars=500)])
    run(monkeypatch, client, lambda rw, r: seen.append(r["id"]) or {"action": "settle", "reason": "kept"},
        env={"AW_SHORT_PER_RUN": "3"})
    assert seen == [1, 2, 9]
    assert {"short": 1, "per_page": 3} in client.gets
    full = Client([row(i) for i in range(1, 6)], [row(9, why="rules")])
    run(monkeypatch, full, lambda rw, r: {"action": "settle", "reason": "kept"})
    assert not any(g.get("short") for g in full.gets)  # the batch is full of new items: no short ones


def test_an_older_site_without_short_listing_is_left_alone(monkeypatch):
    seen = []
    client = Client([row(1)], [row(9, why="rules")], site_has_short=False)
    run(monkeypatch, client, lambda rw, r: seen.append(r["id"]) or {"action": "settle", "reason": "kept"})
    assert seen == [1]


def test_recheck_without_more_text_only_records_the_read(monkeypatch, capsys):
    client = Client([], [row(9, why="recheck", src_chars=1000)])
    pages = {"https://x.test/9": {"text": "x" * 1200, "status": 200}}
    run(monkeypatch, client, lambda rw, r: (_ for _ in ()).throw(AssertionError("rewritten")), pages=pages)
    assert client.posts == [{"id": 9, "before_md5": "m", "recheck": 1200}]
    stats = json.loads(next(line for line in capsys.readouterr().out.splitlines() if line.startswith("RUN "))[4:])
    assert stats["rechecked"] == 1


def test_recheck_with_more_text_is_rewritten_from_that_read(monkeypatch):
    got = []
    client = Client([], [row(9, why="recheck", src_chars=1000)])
    pages = {"https://x.test/9": {"text": "y" * 3000, "status": 200}}

    def process_(rw, r):
        got.append(r["_page"]["text"])
        rw.info = {"chars": 3000}
        return {"action": "write", "summary": "New.", "state": "done", "model": "m", "also": [], "reason": "",
                "before_words": 2, "after_words": 1}

    run(monkeypatch, client, process_, pages=pages)
    assert got == ["y" * 3000]
    assert client.posts[0]["summary"] == "New." and client.posts[0]["src_chars"] == 3000


def test_unknown_earlier_length_is_never_a_reason_to_rewrite():
    assert not sr.finds_more({"text": "z" * 50000}, None)
    assert sr.finds_more({"text": "z" * 2000}, 1000) and not sr.finds_more({"text": "z" * 1500}, 1000)


def test_a_short_item_that_fails_again_stays_short(monkeypatch):
    client = Client([], [row(9, why="rules", src_chars=800)])
    run(monkeypatch, client, lambda rw, r: {"action": "failed", "reason": "names not in the reports: X"})
    assert client.posts == [{"id": 9, "before_md5": "m", "settle": "tried again: names not in the reports: X"}]
