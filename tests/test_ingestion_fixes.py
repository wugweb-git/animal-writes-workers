"""Fixes from the 12 Oct 2026 ingestion audit (no new News 10-12 Oct): cap after the seen-check, newest first,
keyword searches, Bing fallback, failing sources, state saved per item, heartbeat and relevance alert payloads."""

from datetime import date, timedelta
from urllib.parse import parse_qs, urlsplit

from aw_workers import ingest, policy, run, sources
from aw_workers.summarise import DryRunSummariser

TODAY = date(2026, 10, 12)


def _src(sid="gnews-x", url="https://news.google.com/rss/search?q=%22animal+welfare%22+India&hl=en-IN&gl=IN&ceid=IN:en", **kw):
    return sources.Source(id=sid, name=sid, kind="rss", type="news", url=url, **kw)


def _item(src, n, when):
    return sources.Item(source=src, title=f"Stray dog story number {n}", url=f"https://example.com/{n}", date=when,
                        text="A dog was rescued.")


# ---------- cap after the seen-check, newest first ----------

def test_seen_items_no_longer_hide_new_ones(monkeypatch):
    src = _src(max_items=5)
    old = [_item(src, n, TODAY - timedelta(days=3)) for n in range(20)]  # relevance order: old, already seen
    new = [_item(src, 100 + n, TODAY) for n in range(3)]
    monkeypatch.setitem(sources.PARSERS, "rss", lambda s, body: old + new)
    seen = {i.url for i in old}
    stats = run.run([src], {"max_items_per_source": 15}, dry_run=True, summariser=DryRunSummariser(), client=None,
                    fetch=lambda u: "", today=TODAY, seen=seen)
    assert stats.new == 3 and stats.duplicate == 20


def test_collect_sorts_newest_first():
    src = _src()
    body = """<rss><channel>
      <item><title>Old dog</title><link>https://a.in/1</link><pubDate>Mon, 05 Oct 2026 10:00:00 GMT</pubDate></item>
      <item><title>New dog</title><link>https://a.in/2</link><pubDate>Mon, 12 Oct 2026 10:00:00 GMT</pubDate></item>
    </channel></rss>"""
    assert [i.title for i in sources.collect(src, body, TODAY, 14, 1)] == ["New dog"]


def test_cap_per_source():
    src = _src(max_items=2)
    items = [_item(src, n, TODAY) for n in range(5)]
    stats = run.Stats()
    import aw_workers.sources as S
    orig = S.parse_rss
    try:
        S.PARSERS["rss"] = lambda s, body: items
        stats = run.run([src], {}, dry_run=True, summariser=DryRunSummariser(), client=None, fetch=lambda u: "", today=TODAY)
    finally:
        S.PARSERS["rss"] = orig
    assert stats.new == 2


# ---------- keyword searches ----------

def test_keyword_searches_add_one_search_per_english_keyword():
    base = [_src("gnews-animal-welfare")]
    out = run.keyword_searches(base, {"keywords": ["animal welfare", "rabies", "पशु", " "]})
    ids = [s.id for s in out]
    # "animal welfare" is already a source (same query): fetched once; Hindi and blanks are not searched
    assert ids == ["gnews-animal-welfare", "gnews-kw-rabies", "gnews-kw-stray-dogs"]
    q = parse_qs(urlsplit(out[1].url).query)
    assert q["q"] == ['"rabies" India when:2d'] and q["gl"] == ["IN"]
    assert all(s.max_items == run.SEARCH_MAX_ITEMS for s in out)


def test_existing_google_searches_get_a_date_window_once():
    src = _src(url="https://news.google.com/rss/search?q=site:livelaw.in+dogs+when:7d&hl=en-IN")
    out = run.keyword_searches([src], {"keywords": []})
    q = parse_qs(urlsplit(out[0].url).query)["q"][0]
    assert q.count("when:") == 1 and "when:7d" in q
    other = run.keyword_searches([_src()], {})[0]
    assert parse_qs(urlsplit(other.url).query)["q"][0].endswith("when:2d")


def test_non_google_sources_are_untouched():
    hindu = sources.Source(id="the-hindu", name="h", kind="rss", type="news", url="https://www.thehindu.com/feed.rss")
    assert run.keyword_searches([hindu], {})[0] == hindu


# ---------- Google failing: retries, Bing fallback, failing run ----------

class Resp:
    def __init__(self, code, text=""):
        self.status_code, self.text, self.encoding = code, text, "utf-8"

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"{self.status_code} Server Error")


def test_fetch_retries_search_engines_on_503():
    calls = []

    def get(url, headers, timeout):
        calls.append(headers["User-Agent"])
        return Resp(503) if len(calls) < 3 else Resp(200, "ok")

    assert sources.fetch("https://news.google.com/rss/search?q=x", get=get, sleep=lambda s: None) == "ok"
    assert len(calls) == 3 and "Mozilla" in calls[0]


def test_fetch_does_not_retry_other_sites():
    calls = []

    def get(url, headers, timeout):
        calls.append(1)
        return Resp(503)

    try:
        sources.fetch("https://www.thehindu.com/feed", get=get, sleep=lambda s: None)
    except RuntimeError:
        pass
    assert calls == [1]


def test_bing_fallback_url():
    alt = sources.bing_fallback(_src(url="https://news.google.com/rss/search?q=%22stray+dogs%22+India+when%3A2d&hl=en-IN"))
    q = parse_qs(urlsplit(alt.url).query)
    assert alt.url.startswith("https://www.bing.com/news/search?") and q["q"] == ['"stray dogs" India']
    assert q["format"] == ["rss"] and q["mkt"] == ["en-IN"] and alt.lookback_days == 3
    assert sources.bing_fallback(sources.Source(id="h", name="h", kind="rss", type="news", url="https://thehindu.com/f")) is None


BING = """<?xml version="1.0"?><rss version="2.0" xmlns:News="https://www.bing.com/news"><channel>
<item><title>Stray dogs kill 23 sheep in Chevella</title>
<link>http://www.bing.com/news/apiclick.aspx?ref=FexRss&amp;aid=&amp;url=https%3a%2f%2fwww.deccanchronicle.com%2fstray-dogs-1995059&amp;mkt=en-in</link>
<description>At least 23 sheep died after stray dogs attacked them.</description>
<pubDate>Sun, 11 Oct 2026 22:15:00 GMT</pubDate><News:Source>Deccan Chronicle</News:Source></item>
</channel></rss>"""


def test_google_down_falls_back_to_bing():
    def fetch(url):
        if "news.google.com" in url:
            raise RuntimeError("503 Server Error: Service Unavailable")
        return BING

    stats = run.run([_src()], {}, dry_run=True, summariser=DryRunSummariser(), client=None, fetch=fetch, today=TODAY)
    assert (stats.new, stats.sources, stats.sources_failed, stats.failed) == (1, 1, 0, 0)


def test_bing_links_and_publisher():
    item = sources.parse_rss(_src(), BING)[0]
    assert item.url == "https://www.deccanchronicle.com/stray-dogs-1995059" and item.publisher == "Deccan Chronicle"


def test_half_the_sources_failing_fails_the_run():
    def fetch(url):
        raise RuntimeError("503 Server Error")

    hindu = sources.Source(id="h", name="h", kind="rss", type="news", url="https://www.thehindu.com/f")
    stats = run.run([_src(), hindu], {}, dry_run=True, summariser=DryRunSummariser(), client=None, fetch=fetch, today=TODAY)
    assert (stats.sources, stats.sources_failed) == (2, 2) and run.sources_failing(stats)
    assert not run.sources_failing(run.Stats(sources=10, sources_failed=4))


# ---------- progress kept item by item; time budget ----------

def test_checkpoint_after_each_item(monkeypatch):
    src = _src()
    monkeypatch.setitem(sources.PARSERS, "rss", lambda s, body: [_item(src, n, TODAY) for n in range(3)])
    saves = []

    class Client:
        last = {}

        def post(self, payload):
            self.last = {"status": "created", "review": "published"}
            return "created"

    stats = run.run([src], {}, dry_run=False, summariser=DryRunSummariser(), client=Client(), fetch=lambda u: "",
                    today=TODAY, checkpoint=lambda: saves.append(1))
    assert stats.new == 3 and stats.published == 3 and len(saves) == 3


def test_time_budget_stops_cleanly(monkeypatch):
    import time

    src = _src()
    monkeypatch.setitem(sources.PARSERS, "rss", lambda s, body: [_item(src, n, TODAY) for n in range(3)])
    stats = run.run([src, src], {}, dry_run=True, summariser=DryRunSummariser(), client=None, fetch=lambda u: "",
                    today=TODAY, deadline=time.monotonic() - 1)
    assert stats.stopped_early and stats.new == 0 and stats.sources == 0


# ---------- animal words ----------

def test_animal_words_added_12_oct():
    assert policy.about_animals("Around 100 blackbucks rescued & translocated in Shajapur")
    assert policy.about_animals("Orangutan rescued from Maharashtra flat")
    assert not policy.about_animals("NEET UG stray vacancy round registration opens")


# ---------- heartbeat and relevance alert ----------

class Session:
    def __init__(self):
        self.sent = []

    def request(self, method, url, timeout=None, **kw):
        self.sent.append((method, url, kw.get("json")))

        class R:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {"ok": True}

        return R()


def test_heartbeat_payload(monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    s = Session()
    c = ingest.Client("https://site.test", "u", "p", session=s)
    c.ingest_heartbeat(5, published=3, sources=30, sources_failed=2, errors=["gnews-x: 503"])
    method, url, body = s.sent[0]
    assert method == "POST" and url == "https://site.test/wp-json/aw/v1/ingest-heartbeat"
    assert body == {"new": 5, "published": 3, "sources": 30, "sources_failed": 2, "errors": ["gnews-x: 503"],
                    "link": "https://github.com/o/r/actions/runs/42"}


def test_heartbeat_without_published_still_works():
    s = Session()
    ingest.Client("https://site.test", "u", "p", session=s).ingest_heartbeat(0)
    assert "published" not in s.sent[0][2] and s.sent[0][2]["new"] == 0


def test_relevance_alert_payload(monkeypatch):
    monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
    s = Session()
    ingest.Client("https://site.test", "u", "p", session=s).relevance_alert(4)
    method, url, body = s.sent[0]
    assert url.endswith("/aw/v1/relevance-alert") and body == {"held": 4, "run": "", "link": ""}


def test_post_keeps_the_sites_answer():
    class S:
        def post(self, url, json, timeout):
            class R:
                status_code = 201

                def json(self):
                    return {"status": "created", "id": 7, "review": "published"}

            return R()

    c = ingest.Client("https://site.test", "u", "p", session=S())
    assert c.post({"title": "x"}) == "created" and c.last["review"] == "published"


def test_fetch_asks_again_as_a_browser_on_403():
    calls = []

    def get(url, headers, timeout):
        calls.append(headers["User-Agent"])
        return Resp(403) if len(calls) == 1 else Resp(200, "ok")

    assert sources.fetch("https://peopleforanimals.substack.com/feed", get=get, sleep=lambda s: None) == "ok"
    assert calls[0] == sources.USER_AGENT and "Mozilla" in calls[1]


def test_site_search_fallback_for_refused_feeds():
    alt = sources.site_search_fallback(_src(url="https://peopleforanimals.substack.com/feed"))
    q = parse_qs(urlsplit(alt.url).query)
    assert alt.url.startswith("https://news.google.com/rss/search?") and q["q"] == ["site:peopleforanimals.substack.com when:7d"]
    assert sources.site_search_fallback(_src(url="https://news.google.com/rss/search?q=x")) is None


def test_ca_bundle_has_the_missing_intermediates():
    text = open(sources.ca_bundle()).read()
    for pem in ("lets-encrypt-yr1.pem", "lets-encrypt-yr2.pem", "emsign-dv-tls-g2a-1.pem"):
        body = open(f"aw_workers/certs/{pem}").read().strip()
        assert body in text
