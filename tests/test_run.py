from datetime import date

import pytest

from aw_workers import ingest, run, sources
from aw_workers.summarise import DryRunSummariser

TODAY = date(2026, 10, 5)


def all_sources():
    """Feed sources from sources.yaml (document sources fetch their own data and have their own tests)."""
    loaded, defaults = sources.load_sources("sources.yaml")
    return [s for s in loaded if s.kind not in sources.SELF_FETCH], defaults


def fake_fetch(fixture_text):
    files = {"rss": "google_news.xml", "awbi": "awbi_circulars.html"}

    def fetch(url):
        if "pib.gov.in" in url:  # PIB source URL is a Google News query containing site:pib.gov.in
            return fixture_text("pib_sample.xml")
        if "livelaw" in url or "barandbench" in url or "scobserver" in url:
            return fixture_text("pib.xml")  # no animal keywords -> 0 items
        if "thehindu" in url:
            return fixture_text("pib.xml")  # nothing matches the keywords -> 0 items
        return fixture_text(files["awbi" if "awbi" in url else "rss"])

    return fetch


class Boom:
    def post(self, payload):
        raise AssertionError("DRY_RUN must not POST")


def test_dry_run_end_to_end(fixture_text, caplog):
    src, defaults = all_sources()
    caplog.set_level("INFO")
    stats = run.run(
        src, {**defaults, "lookback_days": 3650}, dry_run=True, summariser=DryRunSummariser(),
        client=Boom(), fetch=fake_fetch(fixture_text), today=TODAY,
    )
    assert stats.fetched > 0 and stats.new == stats.fetched and stats.failed == 0
    assert "DRY_RUN would POST" in caplog.text
    assert '"type": "legal"' in caplog.text and '"type": "news"' in caplog.text
    assert '"topics": [' in caplog.text


def test_broken_source_is_counted_not_fatal(fixture_text):
    src, defaults = all_sources()

    def fetch(url):
        if "awbi" in url:
            raise OSError("down")
        return fake_fetch(fixture_text)(url)

    stats = run.run(src, defaults, dry_run=True, summariser=DryRunSummariser(), client=None, fetch=fetch, today=TODAY)
    assert stats.failed == sum("awbi" in s.url for s in src) and stats.fetched > 0


class FakeResp:
    def __init__(self, code, data):
        self.status_code, self._data = code, data

    def json(self):
        return self._data


class FakeSession:
    def __init__(self, responses):
        self.responses, self.sent = list(responses), []

    def post(self, url, json, timeout):
        self.sent.append(json)
        return self.responses.pop(0)


def test_live_counts_created_and_duplicate(fixture_text):
    src = [s for s in all_sources()[0] if s.id == "pib-releases"]
    session = FakeSession([FakeResp(201, {"status": "created", "id": 1}), FakeResp(200, {"status": "duplicate", "id": 1})])
    client = ingest.Client("https://site.test", "ingest", "x", session=session)
    seen = set()
    stats = run.run(src, {}, dry_run=False, summariser=DryRunSummariser(), client=client,
                    fetch=fake_fetch(fixture_text), today=TODAY, seen=seen)
    assert (stats.new, stats.duplicate, stats.failed) == (1, 1, 0)
    assert len(seen) == 2
    assert set(session.sent[0]) == {"type", "title", "summary", "source_url", "source_name", "source_date", "topics", "source_type", "feed"}
    assert session.sent[0]["source_type"] == "pib"
    assert session.sent[0]["type"] == "legal" and session.sent[0]["source_date"] == "2026-10-05"
    # second run: both already seen, nothing POSTed
    stats2 = run.run(src, {}, dry_run=False, summariser=DryRunSummariser(), client=client,
                     fetch=fake_fetch(fixture_text), today=TODAY, seen=seen)
    assert stats2.duplicate == 2 and len(session.sent) == 2


@pytest.mark.parametrize("code", [401, 403])
def test_auth_failure_stops_run(fixture_text, code):
    src = [s for s in all_sources()[0] if s.id == "pib-releases"]
    client = ingest.Client("https://site.test", "ingest", "x", session=FakeSession([FakeResp(code, {"code": "rest_forbidden"})]))
    with pytest.raises(ingest.AuthError):
        run.run(src, {}, dry_run=False, summariser=DryRunSummariser(), client=client,
                fetch=fake_fetch(fixture_text), today=TODAY)


def test_dry_run_default_on(monkeypatch):
    assert run.truthy(None, default=True) is True
    assert run.truthy("", default=True) is True
    assert run.truthy("false", default=True) is False


def test_court_type_from_headline():
    assert ingest.court_type("Supreme Court stays order on community dogs") == "supreme-court"
    assert ingest.court_type("Delhi HC seeks reply on cattle transport") == "high-court"
    assert ingest.court_type("Bombay High Court on feeding spots") == "high-court"
    assert ingest.court_type("NGT notice over bird deaths") == "national-green-tribunal"
    assert ingest.court_type("Ministry issues advisory") == ""


def test_court_type_prefers_deciding_court():
    t = "Public Suffering Due To Authorities' Failure To Implement SC Directions On Stray Dogs: Delhi High Court"
    assert ingest.court_type(t) == "high-court"
    assert ingest.court_type("Cattle Transport Can't Be Presumed To Be For Slaughter: Supreme Court") == "supreme-court"
    assert ingest.court_type("Supreme Court upholds High Court order on feeding") == "supreme-court"
