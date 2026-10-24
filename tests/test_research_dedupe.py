import pytest

from aw_workers import ingest, research


def _p(url):
    return {"url": url, "title": "t", "date": "2026-10-01"}


class _Client:
    def __init__(self, dois=(), urls=(), fail=None, studies=()):
        self.dois, self.urls, self.fail, self.calls, self.studies = set(dois), set(urls), fail, [], list(studies)

    def exists(self, dois, urls, index=False):
        self.calls.append((list(dois), list(urls)))
        if self.fail:
            raise self.fail
        out = {"dois": [d for d in dois if d in self.dois], "urls": [u for u in urls if u in self.urls]}
        if index:
            out["studies"] = self.studies
        return out


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


def test_research_without_a_headline_is_held_not_published():
    raw = {"type": "research", "title": "Effects of organized guarding on mortality from human elephant conflict in northeast India"}
    held = research.with_headline(dict(raw), {})  # the headline step failed (e.g. 85 characters)
    assert held["hold"] == research.NO_HEADLINE and held["title"] == raw["title"]
    ok = research.with_headline(dict(raw), {"title": "Human elephant conflict guarding in Assam and elephant deaths, study",
                                            "seo_title": "Human Elephant Conflict Guarding in Assam", "focus_keyword": "human elephant conflict",
                                            "slug": "human-elephant-conflict-guarding-assam-deaths-study"})
    assert "hold" not in ok and len(ok["title"]) <= 70
    both = research.with_headline(dict(raw, hold="India not confirmed"), {})
    assert both["hold"] == "India not confirmed; " + research.NO_HEADLINE


# 12573 on the site (Europe PMC record), as aw/v1/exists returns it; 20054 was the same study again from the journal DOI.
SITE_12573 = {"id": 12573, "title": "Effects of organized guarding on mortality from human–elephant conflict in northeast India",
              "first_author": "Nitin Sekar", "year": "2026"}
PAPER_20054 = {"url": "https://doi.org/10.1111/cobi.70204", "date": "2026-01-07",
               "title": "Effects of organized guarding on mortality from human elephant conflict in northeast India",
               "authors": ["Nitin Sekar", "Tanay Bhatt", "E. Somanathan"]}


def test_regression_12573_20054_same_study_under_another_doi_is_skipped():
    assert research.same_study(PAPER_20054, [SITE_12573]) == 12573
    known = set()
    todo, already = research.drop_existing([PAPER_20054], _Client(studies=[SITE_12573]), known)
    assert todo == [] and already == 1 and "10.1111/cobi.70204" in known


def test_similar_title_needs_the_same_first_author_or_year():
    other = dict(PAPER_20054, url="https://doi.org/10.1/z", date="2024-03-01", authors=["Someone Else"])
    assert research.same_study(other, [SITE_12573]) == 0  # title alike, but another author and another year
    assert research.same_study(dict(other, date="2026-05-01"), [SITE_12573]) == 12573  # same year
    unlike = dict(PAPER_20054, title="Elephant corridors and railway deaths in Assam")
    assert research.same_study(unlike, [SITE_12573]) == 0


def test_doi_trailing_slash_is_the_same_doi():
    assert research.doi_of("https://doi.org/10.1111/COBI.70204/") == research.doi_of("https://doi.org/10.1111/cobi.70204")
