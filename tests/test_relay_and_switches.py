import base64

from aw_workers import enrich, ingest, merge, relay, sources


class Resp:
    def __init__(self, status_code, text="", data=None):
        self.status_code, self.text, self._data = status_code, text, data

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"{self.status_code} Client Error")


class FakeSession:
    def __init__(self, body=b"<rss>ok</rss>", status=200, site_status=200):
        self.calls, self.body, self.status, self.site_status = [], body, status, site_status

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        data = {"status": self.status, "content_type": "application/xml; charset=utf-8", "body": base64.b64encode(self.body).decode()}
        return Resp(self.site_status, "", data)


class FakeClient:
    def __init__(self, session):
        self.session, self.url = session, "https://animalwrites.in/wp-json/aw/v1/ingest"


def setup_function():
    relay.configure(None, ())


def refused(url, headers, timeout):
    return Resp(403)


def test_feed_refused_from_github_is_read_through_the_site():
    s = FakeSession()
    relay.configure(FakeClient(s), ["substack.com"])
    assert sources.fetch("https://peopleforanimals.substack.com/feed", get=refused, sleep=lambda x: None) == "<rss>ok</rss>"
    assert s.calls == [("https://animalwrites.in/wp-json/aw/v1/relay", {"url": "https://peopleforanimals.substack.com/feed"})]


def test_hosts_not_on_the_list_are_never_relayed():
    s = FakeSession()
    relay.configure(FakeClient(s), ["substack.com"])
    try:
        sources.fetch("https://www.thehindu.com/feed", get=refused, sleep=lambda x: None)
    except RuntimeError:
        pass
    assert s.calls == [] and not relay.allowed("https://notsubstack.com/feed")


def test_relay_failure_falls_back_to_the_403():
    relay.configure(FakeClient(FakeSession(site_status=403)), ["substack.com"])
    try:
        sources.fetch("https://x.substack.com/feed", get=refused, sleep=lambda x: None)
        raise AssertionError("expected the 403")
    except RuntimeError as exc:
        assert "403" in str(exc)


def test_page_text_and_picture_through_the_site():
    page = (b'<html><head><meta property="og:image" content="https://images.zoomnews.com/photo/1.jpg">'
            b'<meta name="description" content="Actors oppose the draft rules on animal testing in laboratories."></head>'
            b"<body><article><p>Kalki Koechlin, Abhirami and Nafisa Ali have urged people to send objections to CCSEA.</p></article></body></html>")
    relay.configure(FakeClient(FakeSession(body=page)), ["zoomtventertainment.com"])

    class Refusing:
        def get(self, url, headers=None, timeout=None):
            return Resp(403, "Forbidden")

    url = "https://www.zoomtventertainment.com/bollywood/story-article-1"
    assert "urged people to send objections" in merge.page_text(url, session=Refusing())
    assert enrich.og_image(url, session=Refusing()) == "https://images.zoomnews.com/photo/1.jpg"


def _lists(**src):
    base = {"id": "feed", "name": "Feed", "kind": "rss", "type": "news", "url": "https://example.com/feed", "keywords": ["own"]}
    return {"keywords": ["animal welfare"], "sources": [{**base, **src}]}


def test_source_switched_off_is_not_fetched():
    assert sources.load_sources_data(_lists(on=False))[0] == []
    assert len(sources.load_sources_data(_lists(on=True))[0]) == 1


def test_keyword_check_gives_the_keyword_list():
    assert sources.load_sources_data(_lists(filter=True))[0][0].keywords == ["animal welfare"]
    assert sources.load_sources_data(_lists(filter=False))[0][0].keywords == ["own"]
    assert sources.load_sources_data(_lists())[0][0].keywords == ["own"]  # sources.yaml: read as it is


def test_heartbeat_says_which_lists_were_used():
    sent = {}
    c = ingest.Client.__new__(ingest.Client)
    c._call = lambda method, path, **kw: sent.update(kw["json"]) or {}
    c.ingest_heartbeat(3, published=2, lists="site")
    assert sent["lists"] == "site" and sent["published"] == 2


def test_unreachable_feed_is_read_through_the_site():
    import requests

    s = FakeSession(body=b"<table>rows</table>")
    relay.configure(FakeClient(s), ["awbi.gov.in"])

    def timeout(url, headers, timeout):
        raise requests.ConnectTimeout("connect timeout")

    assert sources.fetch("https://awbi.gov.in/circulars", get=timeout, sleep=lambda x: None) == "<table>rows</table>"
    relay.configure(FakeClient(s), ["substack.com"])
    try:
        sources.fetch("https://awbi.gov.in/circulars", get=timeout, sleep=lambda x: None)
        raise AssertionError("expected the timeout")
    except requests.ConnectTimeout:
        pass


def test_relay_session_is_get_only_and_raises_like_requests():
    import requests

    relay.configure(FakeClient(FakeSession(status=404)), ["egazette.gov.in"])
    sess = relay.Session()
    resp = sess.get("https://egazette.gov.in/RecentUploads.aspx?Category=6")
    assert resp.status_code == 404
    try:
        resp.raise_for_status()
        raise AssertionError("expected HTTPError")
    except requests.HTTPError:
        pass
    relay.configure(None, ())
    try:
        relay.Session().get("https://egazette.gov.in/")
        raise AssertionError("expected ConnectionError")
    except requests.ConnectionError:
        pass

