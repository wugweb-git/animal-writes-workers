"""Search Insights phase 3 (aw_workers/search_insights.py): parsing, spike rules, pacing, one attempt, payloads."""
import json
from datetime import date, timedelta

import pytest

from aw_workers import search_insights as si

TODAY = date(2026, 10, 24)


class Resp:
    def __init__(self, status=200, body="", url="https://example.org/"):
        self.status_code = status
        self.text = body if isinstance(body, str) else json.dumps(body)
        self.content = self.text.encode()
        self.url = url

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class Session:
    """Answers by URL substring, in order; records every request."""

    def __init__(self, routes):
        self.routes = routes
        self.headers = {}
        self.calls = []

    def get(self, url, timeout=None, params=None):
        self.calls.append((url, params))
        for key, answer in self.routes:
            if key in url:
                return answer(url, params) if callable(answer) else answer
        raise AssertionError(f"unexpected request {url}")


def fetcher(routes, sleeps=None):
    sleeps = [] if sleeps is None else sleeps
    return si.Fetcher(Session(routes), sleep=sleeps.append, rand=lambda a, b: a)


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setattr(si, "STATE", tmp_path / "search-insights")


def test_norm_matches_the_site():
    assert si.norm("Stray Dogs") == "stray dogs"  # 4-letter words keep their s on the site too ([a-z]{4,}s)
    assert si.norm("Leopards") == "leopard"
    assert si.norm("G.S.R. 193(E)") == "gsr 193 e"
    assert si.norm("rabies news") == "rabies news"
    assert si.norm("Gaushalas in U.P.") == "gaushala in up"
    assert si.norm("Animal Welfare Board's") == "animal welfare board"  # same output as the site's aw_si_norm (checked with PHP)


def test_english_only():
    assert si.english("stray dogs near me")
    assert si.english("Human–wildlife conflict")
    assert not si.english("आवारा कुत्ते")
    assert not si.english("")


def test_vocabulary_drops_generic_single_words():
    v = dict(si.vocabulary({"species": ["dog", "india"], "topics": ["animal welfare", "rabies"], "keywords": ["abc rules {district}", "animal"]}))
    assert v == {"dog": "species", "rabies": "topics"}  # "animal welfare" is only generic words; placeholders go


RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:ht="https://trends.google.com/trending/rss" version="2.0"><channel>
<item><title>leopard attack pune</title><ht:approx_traffic>2,000+</ht:approx_traffic><pubDate>Sat, 24 Oct 2026 09:40:00 -0700</pubDate></item>
<item><title>ipl auction</title><ht:approx_traffic>50,000+</ht:approx_traffic><pubDate>Sat, 24 Oct 2026 09:40:00 -0700</pubDate>
  <ht:news_item><ht:news_item_title>Teams bid big at auction</ht:news_item_title></ht:news_item></item>
<item><title>supreme court order</title><ht:approx_traffic>500+</ht:approx_traffic><pubDate>Fri, 23 Oct 2026 22:00:00 -0700</pubDate>
  <ht:news_item><ht:news_item_title>Supreme Court revisits stray dogs relocation</ht:news_item_title></ht:news_item></item>
<item><title>old dog story</title><ht:approx_traffic>500+</ht:approx_traffic><pubDate>Mon, 12 Oct 2026 09:00:00 -0700</pubDate></item>
<item><title>कुत्ता</title><ht:approx_traffic>500+</ht:approx_traffic><pubDate>Sat, 24 Oct 2026 09:40:00 -0700</pubDate></item>
</channel></rss>"""


def test_trends_rss_keeps_only_our_vocabulary():
    f = fetcher([("trending/rss", Resp(body=RSS))])
    out = si.trends_rss(f, {"species": ["leopard", "dog"], "topics": ["stray dogs"], "keywords": []}, TODAY, days=1)
    terms = [r["term"] for r in out["rising"]]
    assert terms == ["leopard attack pune", "supreme court order"]  # ipl: no match; old: outside the window; Hindi: phase 5
    assert "species “leopard”" in out["rising"][0]["note"]
    assert out["rising"][1]["note"].endswith("in its news")
    assert out["obs"][0] == {"term": "leopard attack pune", "metric": "traffic", "value": 2000, "period": "day", "d": "2026-10-24", "txt": "2,000+"}
    assert out["info"] == {"trending": 5, "kept": 2}
    assert len(f.session.calls) == 1  # one request


def _views(days_values):
    return {"items": [{"timestamp": f"{d:%Y%m%d}00", "views": v} for d, v in days_values]}


def test_spike_rule():
    day = TODAY
    s = {day - timedelta(days=i): 100 for i in range(1, 31)}
    s[day] = 199
    assert si.spikes(s, day, 30, 2.0, 50) is None
    s[day] = 200
    assert si.spikes(s, day, 30, 2.0, 50) == 2.0
    assert si.spikes({day: 40}, day, 30, 2.0, 50) is None  # under the minimum
    assert si.spikes({day: 60}, day, 30, 2.0, 50) == float("inf")  # no history: a spike with no ratio


def test_wikipedia_flags_two_times_the_30_day_average_and_paces():
    end = TODAY - timedelta(days=1)
    series = [(end - timedelta(days=i), 100) for i in range(1, 37)] + [(end, 260)]
    sleeps = []
    f = fetcher([("/Rabies/", Resp(body=_views(series))), ("/Gaushala/", Resp(404, "{}"))], sleeps)
    out = si.wikipedia(f, {"wikipedia": ["Rabies", "Gaushala"]}, TODAY, days=7)
    assert [(r["term"], r["growth"], r["d"]) for r in out["rising"]] == [("Rabies", 2.6, end.isoformat())]
    assert len([o for o in out["obs"] if o["term"] == "Rabies"]) == 7
    assert out["info"]["missing"] == ["Gaushala"]
    assert f.session.headers["User-Agent"].startswith("AnimalWritesSearchInsights/1.0 (") and "@" in f.session.headers["User-Agent"]
    assert len(sleeps) == 1 and 0 < sleeps[0] <= si.WIKI_PAUSE  # at most one request a second
    url = f.session.calls[0][0]
    assert url.endswith(f"/daily/{end - timedelta(days=36):%Y%m%d}/{end:%Y%m%d}")


def _gdelt(points):
    return {"timeline": [{"series": "Article Count", "data": [{"date": f"{d:%Y%m%d}T000000Z", "value": v} for d, v in points]}]}


def test_gdelt_flags_three_times_the_14_day_average_and_waits_five_seconds():
    end = TODAY - timedelta(days=1)
    hot = [(end - timedelta(days=i), 2) for i in range(1, 16)] + [(end, 6)]
    cold = [(end - timedelta(days=i), 4) for i in range(0, 16)]
    sleeps = []
    f = fetcher([("%22rabies%22", Resp(body=_gdelt(hot))), ("%22gaushala%22", Resp(body=_gdelt(cold)))], sleeps)
    out = si.gdelt(f, {"gdelt": ["rabies", "gaushala"]}, TODAY, days=1)
    assert [(r["term"], r["growth"]) for r in out["rising"]] == [("rabies", 3.0)]
    assert sleeps and sleeps[0] > 5
    assert "sourcecountry%3AIN" in f.session.calls[0][0] and "timelinevolraw" in f.session.calls[0][0]
    assert out["error"] == ""


def test_gdelt_skips_a_failed_topic_and_reports_a_failed_day():
    f = fetcher([("gdeltproject", Resp(body="Your query was too short"))])
    out = si.gdelt(f, {"gdelt": ["rabies"]}, TODAY, days=1)
    assert out["rising"] == [] and out["error"].startswith("GDELT answered no topic today")


def test_gdelt_refusal_is_one_attempt():
    f = fetcher([("gdeltproject", Resp(429, "Please limit requests"))])
    out = si.run_source("gdelt", f, {"gdelt": ["rabies", "dog bite"]}, TODAY, 1)
    assert "HTTP 429" in out["error"] and len(f.session.calls) == 1


def test_autocomplete_diff_against_the_last_snapshot_and_cache():
    answers = {"stray%20dogs": ["stray dogs", "stray dogs complaint", "stray dogs meaning"]}
    f = fetcher([("suggestqueries", lambda url, p: Resp(body=["q", answers[url.split("q=")[1]]]))])
    first = si.autocomplete(f, {"autocomplete": ["stray dogs"]}, TODAY)
    assert first["rising"] == [] and first["info"]["baseline"] == 1  # the first snapshot is only stored
    answers["stray%20dogs"] = ["stray dogs", "stray dogs complaint", "stray dogs supreme court order", "आवारा कुत्ते"]
    week = TODAY + timedelta(days=7)
    second = si.autocomplete(f, {"autocomplete": ["stray dogs"]}, week)
    assert [r["term"] for r in second["rising"]] == ["stray dogs supreme court order"]
    calls = len(f.session.calls)
    again = si.autocomplete(f, {"autocomplete": ["stray dogs"]}, week + timedelta(days=1))  # a re-run that week: cached
    assert len(f.session.calls) == calls and [r["term"] for r in again["rising"]] == ["stray dogs supreme court order"]
    saved = json.loads((si.STATE / "autocomplete.json").read_text())
    assert saved["stray dogs"]["date"] == week.isoformat()
    assert "gl=in" in f.session.calls[0][0] and "hl=en" in f.session.calls[0][0]


def test_autocomplete_pacing_and_refusal_stops_the_rest():
    sleeps = []
    calls = {"n": 0}

    def answer(url, p):
        calls["n"] += 1
        return Resp(body=["q", ["a b"]]) if calls["n"] < 3 else Resp(429, "", url="https://www.google.com/sorry/index")

    f = fetcher([("suggestqueries", answer)], sleeps)
    out = si.autocomplete(f, {"autocomplete": ["a", "b", "c", "d"]}, TODAY)
    assert calls["n"] == 3 and "after 2 seeds" in out["error"]
    assert all(1.9 <= s <= 4 for s in sleeps) and len(sleeps) == 2


def test_growth_of_trends_values():
    assert si.growth_of("+250%", 250) == (3.5, "+250%")
    assert si.growth_of("Breakout", 5600) == (None, "Breakout (more than +5,000%)")


def _trends_routes(widgets, data):
    return [
        ("trends/explore?", Resp(body="<html></html>")),
        ("api/explore", lambda u, p: Resp(body=")]}'\n" + json.dumps({"widgets": widgets(json.loads(p["req"]))}))),
        ("widgetdata/", lambda u, p: Resp(body=")]}',\n" + json.dumps(data(u, p)))),
    ]


def test_trends_related_rising_queries():
    widgets = lambda req: [{"id": "RELATED_QUERIES", "token": "t", "request": {"k": req["comparisonItem"][0]["keyword"]}}]
    data = lambda u, p: {"default": {"rankedList": [{"rankedKeyword": [{"query": "top"}]}, {"rankedKeyword": [
        {"query": "stray dogs supreme court", "value": 5600, "formattedValue": "Breakout"},
        {"query": "stray dog bite", "value": 140, "formattedValue": "+140%"},
        {"query": "आवारा कुत्ते", "value": 100, "formattedValue": "+100%"}]}]}}
    f = fetcher(_trends_routes(widgets, data))
    out = si.trends_related(f, {"trends_related": ["stray dogs"]}, TODAY)
    assert [(r["term"], r["growth"]) for r in out["rising"]] == [("stray dogs supreme court", None), ("stray dog bite", 2.4)]
    assert out["error"] == ""


def test_trends_refusal_is_one_attempt_with_the_paste_form_named():
    f = fetcher([("trends.google.com", Resp(429, ""))])
    out = si.trends_related(f, {"trends_related": ["stray dogs", "rabies"]}, TODAY)
    assert "HTTP 429" in out["error"] and "Rising tab" in out["error"] and len(f.session.calls) == 1


def test_trends_interest_is_anchored_on_animal_welfare():
    seen = []

    def widgets(req):
        seen.append([c["keyword"] for c in req["comparisonItem"]])
        return [{"id": "TIMESERIES", "token": "t", "request": {}}]

    data = lambda u, p: {"default": {"averages": [20, 40, 5, 0, 10]}}
    f = fetcher(_trends_routes(widgets, data))
    out = si.trends_interest(f, {"interest": ["rabies", "gaushala", "pca act", "dog bite", "animal welfare"], "anchor": "animal welfare"}, TODAY)
    assert seen == [["animal welfare", "rabies", "gaushala", "pca act", "dog bite"]]  # the anchor is never asked as a keyword
    assert [(o["term"], o["value"], o["period"], o["d"]) for o in out["obs"]] == [
        ("rabies", 200, "12m", "2026-10-01"), ("gaushala", 25, "12m", "2026-10-01"), ("pca act", 0, "12m", "2026-10-01"), ("dog bite", 50, "12m", "2026-10-01")]


def test_top_joins_sources_and_sorts_by_growth():
    results = [
        {"source": "wikipedia", "rising": [{"term": "Rabies", "growth": 2.6, "d": "2026-10-22", "note": "w"}]},
        {"source": "gdelt", "rising": [{"term": "rabies", "growth": 3.1, "d": "2026-10-23", "note": "g"},
                                        {"term": "dog bite", "growth": None, "d": "2026-10-23", "note": "g"}]},
    ]
    rows = si.top(results, 10)
    assert rows[0]["term"] == "Rabies" and rows[0]["sources"] == ["wikipedia", "gdelt"] and rows[0]["growth"] == 3.1
    assert rows[0]["first"] == "2026-10-22" and rows[1]["term"] == "dog bite"


def test_main_dry_run_sends_nothing(monkeypatch, capsys):
    posted = []

    class Client:
        def __init__(self, *a):
            pass

        def search_insights(self):
            return {"wikipedia": ["Rabies"], "gdelt": ["rabies"]}

        def search_insights_post(self, body):
            posted.append(body)
            return {"stored": 1}

    monkeypatch.setattr(si.ingest, "Client", Client)
    monkeypatch.setattr(si, "run_source", lambda name, f, c, t, d: {**si.result(name), "rising": [{"term": "rabies", "growth": 2.0, "d": "2026-10-23", "note": "n"}]})
    monkeypatch.setenv("WP_BASE_URL", "https://x")
    monkeypatch.setenv("WP_INGEST_USER", "ingest")
    monkeypatch.setenv("WP_INGEST_APP_PASSWORD", "p")
    monkeypatch.setenv("DRY_RUN", "true")
    assert si.main(["daily"]) == 0
    assert posted == [] and "| 1 | rabies |" in capsys.readouterr().out
    monkeypatch.setenv("DRY_RUN", "false")
    assert si.main(["daily"]) == 0
    assert [p["source"] for p in posted] == ["trends_rss", "wikipedia", "gdelt"] and posted[0]["dry_run"] is False


def test_main_live_needs_the_site():
    import os

    for k in ("WP_BASE_URL", "WP_INGEST_APP_PASSWORD"):
        os.environ.pop(k, None)
    os.environ["DRY_RUN"] = "false"
    try:
        assert si.main(["daily"]) == 1
    finally:
        os.environ.pop("DRY_RUN")


def test_client_routes(monkeypatch):
    from aw_workers import ingest

    seen = []

    class S:
        auth = None

        def request(self, method, url, timeout=None, **kw):
            seen.append((method, url, kw.get("json")))
            return Resp(body={"ok": True})

    c = ingest.Client("https://site.test/", "ingest", "pw", session=S())
    c.search_insights()
    c.search_insights_post({"source": "gdelt"})
    assert seen == [("GET", "https://site.test/wp-json/aw/v1/search-insights", None),
                    ("POST", "https://site.test/wp-json/aw/v1/search-insights", {"source": "gdelt"})]


def test_our_user_agent_replaces_the_requests_default():
    import requests

    assert si.Fetcher(requests.Session()).session.headers["User-Agent"] == si.UA  # Wikimedia refuses python-requests (403)


def test_exclude_words_drop_off_topic_terms():
    words = si.DEFAULTS["exclude"]
    assert si.excluded("bungo stray dogs season 3", words)
    assert si.excluded("stray dogs movies", words)
    assert si.excluded("stray dogs meaning in hindi", words)
    assert not si.excluded("stray dogs supreme court", words)
    assert not si.excluded("castration of stray dogs", words)  # whole words only
    f = fetcher([])
    fake = lambda f, c, t, d: {**si.result("x"), "rising": [{"term": "stray dogs anime", "growth": None, "d": "2026-10-24", "note": ""},
                                                              {"term": "stray dog bite", "growth": None, "d": "2026-10-24", "note": ""}]}
    si.SOURCES["fake"] = fake
    try:
        out = si.run_source("fake", f, {"exclude": words}, TODAY, 1)
    finally:
        del si.SOURCES["fake"]
    assert [r["term"] for r in out["rising"]] == ["stray dog bite"] and out["info"]["excluded"] == 1


def test_a_refusal_keeps_what_the_source_already_read():
    end = TODAY - timedelta(days=1)
    ok = Resp(body=_gdelt([(end - timedelta(days=i), 2) for i in range(16)]))
    n = {"i": 0}

    def answer(url, p):
        n["i"] += 1
        return ok if n["i"] == 1 else Resp(429, "")

    f = fetcher([("gdeltproject", answer)])
    out = si.run_source("gdelt", f, {"gdelt": ["rabies", "dog bite", "gaushala"]}, TODAY, 1)
    assert [o["term"] for o in out["obs"]] == ["rabies"] and "after 1 topics" in out["error"] and n["i"] == 2


def test_one_bad_wikipedia_title_skips_only_that_page():
    end = TODAY - timedelta(days=1)
    f = fetcher([("/Bad", Resp(400, "{}")), ("/Rabies/", Resp(body=_views([(end, 10)])))])
    out = si.wikipedia(f, {"wikipedia": ["Bad", "Rabies"]}, TODAY, days=1)
    assert out["obs"][0]["term"] == "Rabies" and out["info"]["failed"][0].startswith("Bad") and out["error"] == ""


def test_a_refused_service_is_not_asked_again_in_the_same_run():
    f = fetcher([("trends.google.com", Resp(429, ""))])
    si.run_source("trends_related", f, {"trends_related": ["a"]}, TODAY, 7)
    out = si.run_source("trends_interest", f, {"interest": ["rabies"]}, TODAY, 30)
    assert len(f.session.calls) == 1 and "not asked again" in out["error"]


def test_single_species_word_in_a_headline_is_not_enough():
    rss = RSS.replace("Teams bid big at auction", "India opt to bat first as the tiger roars")
    f = fetcher([("trending/rss", Resp(body=rss))])
    out = si.trends_rss(f, {"species": ["tiger", "leopard"], "topics": [], "keywords": []}, TODAY, days=1)
    assert "ipl auction" not in [r["term"] for r in out["rising"]]


def test_a_dry_run_does_not_move_the_autocomplete_snapshots():
    f = fetcher([("suggestqueries", Resp(body=["q", ["a b"]]))])
    si.autocomplete(f, {"autocomplete": ["a"], "_dry_run": True}, TODAY)
    assert not (si.STATE / "autocomplete.json").exists()


def test_days_must_be_a_small_whole_number():
    import argparse

    assert si._days("7") == 7 and si._days("0") == 0
    for bad in ("7d", "-1", "99"):
        with pytest.raises(argparse.ArgumentTypeError):
            si._days(bad)


def test_scheduled_runs_have_their_own_live_switch():
    from pathlib import Path

    wf = (Path(__file__).resolve().parent.parent / ".github/workflows/search-insights.yml").read_text()
    assert "vars.SI_LIVE == 'true'" in wf and "vars.DRY_RUN" not in wf


class _SiteSession:
    """The site's relay: answers GET aw/v1/relay with the page as the site read it."""

    def __init__(self, status=200, body=None):
        self.calls, self.status, self.body = [], status, body

    def get(self, url, params=None, timeout=None):
        import base64

        self.calls.append(params["url"])
        payload = {"status": self.status, "content_type": "application/json", "body": base64.b64encode(json.dumps(self.body).encode()).decode()}
        return Resp(200, payload)


class _SiteClient:
    def __init__(self, session):
        self.session, self.url = session, "https://animalwrites.in/wp-json/aw/v1/ingest"


def test_gdelt_refused_from_github_is_read_through_the_site():
    from aw_workers import relay

    d = TODAY - timedelta(days=1)
    timeline = {"timeline": [{"series": "Article Count", "data": [{"date": d.strftime("%Y%m%d") + "T000000Z", "value": 7}]}]}
    site = _SiteSession(body=timeline)
    relay.configure(_SiteClient(site), ["gdeltproject.org"])
    try:
        f = fetcher([("gdeltproject", Resp(429, "Please limit requests"))])
        out = si.run_source("gdelt", f, {"gdelt": ["rabies", "dog bite"]}, TODAY, 1)
    finally:
        relay.configure(None, ())
    assert out["error"] == "" and len(site.calls) == 2  # both topics read through the site
    assert len(f.session.calls) == 1  # GitHub's request is made once; after the refusal the rest go through the site
    assert [o["value"] for o in out["obs"]] == [7, 7]


def test_gdelt_refused_by_the_site_too_stops_after_one_attempt():
    from aw_workers import relay

    site = _SiteSession(status=429, body={})
    relay.configure(_SiteClient(site), ["gdeltproject.org"])
    try:
        f = fetcher([("gdeltproject", Resp(429, "Please limit requests"))])
        out = si.run_source("gdelt", f, {"gdelt": ["rabies", "dog bite"]}, TODAY, 1)
    finally:
        relay.configure(None, ())
    assert "HTTP 429" in out["error"] and "through the site: HTTP 429" in out["error"] and len(site.calls) == 1


def test_a_source_with_an_error_and_no_data_fails_the_run(monkeypatch, tmp_path):
    cfg = tmp_path / "lists.json"
    cfg.write_text(json.dumps({"gdelt": ["rabies"]}))
    refused = fetcher([("gdeltproject", Resp(429, "Please limit requests"))])
    monkeypatch.setattr(si, "Fetcher", lambda: refused)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.delenv("WP_BASE_URL", raising=False)
    assert si.main(["daily", "--only", "gdelt", "--config", str(cfg)]) == 1


def test_relay_hosts_come_from_the_site_policy():
    class Policy:
        def __init__(self, raw):
            self.raw = raw

        def policy(self):
            if isinstance(self.raw, Exception):
                raise self.raw
            return self.raw

    assert si.relay_hosts(Policy({"relay_hosts": ["gdeltproject.org", "substack.com"]})) == ["gdeltproject.org", "substack.com"]
    assert si.relay_hosts(Policy({})) == []  # the switch is off on the site: the worker does not try the relay
    assert si.relay_hosts(Policy(RuntimeError("HTTP 500"))) == []
