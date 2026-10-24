import pytest

from aw_workers import ingest, research


def _p(url):
    return {"url": url, "title": "t", "date": "2026-10-01"}


class _Client:
    def __init__(self, dois=(), urls=(), fail=None):
        self.dois, self.urls, self.fail, self.calls = set(dois), set(urls), fail, []

    def exists(self, dois, urls):
        self.calls.append((list(dois), list(urls)))
        if self.fail:
            raise self.fail
        return {"dois": [d for d in dois if d in self.dois], "urls": [u for u in urls if u in self.urls]}


def test_known_dois_and_the_sites_answer_drop_papers_before_any_ai():
    papers = [_p("https://doi.org/10.1/A"), _p("https://doi.org/10.1/b"), _p("https://europepmc.org/article/MED/9"),
              _p("https://doi.org/10.1/c")]
    known = {"10.1/a"}  # from an earlier run (case-insensitive)
    client = _Client(dois={"10.1/b"}, urls={"https://europepmc.org/article/MED/9"})
    todo, already = research.drop_existing(papers, client, known)
    assert [p["url"] for p in todo] == ["https://doi.org/10.1/c"] and already == 3
    assert "10.1/b" in known  # cached for the next run
    assert len(client.calls) == 1 and "10.1/a" not in client.calls[0][0]  # a known DOI is not even asked


def test_batches_of_500():
    papers = [_p(f"https://doi.org/10.1/{i}") for i in range(1100)]
    client = _Client()
    todo, already = research.drop_existing(papers, client, set())
    assert len(todo) == 1100 and already == 0 and [len(c[0]) for c in client.calls] == [500, 500, 100]


def test_site_cannot_answer_keeps_the_papers():
    papers = [_p("https://doi.org/10.1/x"), _p("https://doi.org/10.1/y")]
    todo, already = research.drop_existing(papers, _Client(fail=RuntimeError("404 Client Error")), set())
    assert len(todo) == 2 and already == 0


def test_rejected_login_still_stops_the_run():
    with pytest.raises(ingest.AuthError):
        research.drop_existing([_p("https://doi.org/10.1/x")], _Client(fail=ingest.AuthError("401")), set())


def test_known_dois_round_trip(tmp_path):
    path = tmp_path / "known.json"
    research.save_known({"10.1/b", "10.1/a"}, path)
    assert research.load_known(path) == {"10.1/a", "10.1/b"} and research.load_known(tmp_path / "none.json") == set()
