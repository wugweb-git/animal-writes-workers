from aw_workers import firecrawl

MD = """- [Home](https://x.test/) - [India](https://x.test/india)
![photo](https://x.test/a.jpg)
# Bison scare in Arambol
Mapusa: Panic has gripped the coastal villages of Arambol and Paliem after repeated sightings of a wild bison in [residential areas](https://x.test/r).
Read more | Share
The forest department said its staff were tracking the animal and asked villagers to stay away from it at night.
"""


def test_article_text_keeps_sentences_and_drops_menus():
    text = firecrawl.article_text(MD)
    assert text.startswith("Mapusa: Panic") and "residential areas." in text and "forest department" in text
    assert "Home" not in text and "photo" not in text and "Read more" not in text


class _Resp:
    def __init__(self, code, body=None):
        self.status_code, self._body = code, body or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class _Session:
    def __init__(self, resp):
        self.resp, self.calls = resp, 0

    def post(self, *a, **kw):
        self.calls += 1
        return self.resp


def test_no_key_no_call(monkeypatch):
    monkeypatch.setattr(firecrawl, "_state", {"key": "", "used": 0, "stopped": False})
    s = _Session(_Resp(200))
    assert firecrawl.page_text("https://x.test/a", session=s) == "" and s.calls == 0


def test_out_of_credits_stops_the_run(monkeypatch):
    monkeypatch.setattr(firecrawl, "_state", {"key": "fc-x", "used": 0, "stopped": False})
    s = _Session(_Resp(402))
    assert firecrawl.page_text("https://x.test/a", session=s) == "" and firecrawl.page_text("https://x.test/b", session=s) == ""
    assert s.calls == 1


def test_per_run_limit(monkeypatch):
    monkeypatch.setattr(firecrawl, "_state", {"key": "fc-x", "used": 0, "stopped": False})
    monkeypatch.setenv("FIRECRAWL_MAX_PER_RUN", "2")
    s = _Session(_Resp(200, {"data": {"markdown": MD}}))
    got = [firecrawl.page_text(f"https://x.test/{i}", session=s) for i in range(3)]
    assert got[0].startswith("Mapusa") and got[2] == "" and s.calls == 2
