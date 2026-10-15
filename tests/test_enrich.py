from aw_workers import enrich, ingest, run, sources
from aw_workers.summarise import DryRunSummariser
from datetime import date


class R:
    def __init__(self, text):
        self.text = text


class FakeSession:
    def __init__(self, gets, posts=None):
        self.gets, self.posts = gets, posts or []

    def get(self, url, headers=None, timeout=None):
        return R(self.gets.pop(0))

    def post(self, url, data=None, headers=None, timeout=None):
        return R(self.posts.pop(0))


def test_resolve_google_news():
    page = '<c-wiz data-n-a-sg="SIG" data-n-a-ts="1700000000"></c-wiz>'
    reply = ')]}\'\n[["wrb.fr","Fbv4je","[\\"garturlres\\",\\"https://pub.example/story\\",1]",null]]'
    s = FakeSession([page], [reply])
    assert enrich.resolve_google_news("https://news.google.com/rss/articles/ABC123?oc=5", s) == "https://pub.example/story"


def test_resolve_leaves_other_urls_and_failures_alone():
    assert enrich.resolve_google_news("https://www.thehindu.com/x") == "https://www.thehindu.com/x"
    assert enrich.resolve_google_news("https://news.google.com/rss/articles/ABC", FakeSession(["no signature"])) == \
        "https://news.google.com/rss/articles/ABC"


def test_og_image_variants():
    a = '<meta property="og:image" content="https://img.example/a.jpg" />'
    b = '<meta content="/rel/b.jpg" property="og:image">'
    c = '<meta property="og:image" content="http://insecure/c.jpg">'
    assert enrich.og_image("https://p.example/s", FakeSession([a])) == "https://img.example/a.jpg"
    assert enrich.og_image("https://p.example/s", FakeSession([b])) == "https://p.example/rel/b.jpg"
    assert enrich.og_image("https://p.example/s", FakeSession([c])) == ""
    assert enrich.og_image("https://awbi.gov.in/x.pdf") == ""


def test_payload_carries_image_and_resolved_url(fixture_text):
    src = [s for s in sources.load_sources("sources.yaml")[0] if s.id == "pib-releases"]
    sent = []

    class Client:
        def post(self, payload):
            sent.append(payload)
            return "created"

    seen = set()
    enr = lambda u: (u.replace("https://pib.gov.in/", "https://pub.example/"), "https://img.example/x.jpg")
    run.run(src, {}, dry_run=False, summariser=DryRunSummariser(), client=Client(),
            fetch=lambda u: fixture_text("pib_sample.xml"), today=date(2026, 10, 5), seen=seen, enricher=enr)
    assert sent[0]["image_url"] == "https://img.example/x.jpg"
    assert sent[0]["source_url"].startswith("https://pub.example/")
    assert any(u.startswith("https://pib.gov.in/") for u in seen)  # original kept as seen too


def test_resolve_google_news_keeps_query_string():
    # PIB links carry "?PRID=..."; Google returns "=" and "&" escaped twice.
    page = '<div data-n-a-sg="SIG" data-n-a-ts="123"></div>'
    reply = ')]}\'\n[["wrb.fr","Fbv4je","[\\"garturlres\\",\\"https://www.pib.gov.in/PressReleaseDetail.aspx?PRID\\\\u003d2318504\\\\u0026lang\\\\u003d1\\",1]",null]]'
    s = FakeSession([page], [reply])
    assert enrich.resolve_google_news("https://news.google.com/rss/articles/ABC123?oc=5", s) == \
        "https://www.pib.gov.in/PressReleaseDetail.aspx?PRID=2318504&lang=1"


def test_pick_categories_keywords_then_model():
    from aw_workers import summarise, sources
    sources.CATEGORIES.clear(); sources.CATEGORIES.update({"community-dogs": ["stray dog"], "adoption": ["adopt"], "policy-government": ["ministry"]})
    src = sources.Source(id="x", name="X", kind="rss", type="news", url="u")
    item = sources.Item(src, "Stray dog census begins", "https://e/1", None)
    assert summarise.pick_categories(None, item, "", list(sources.CATEGORIES)) == ["community-dogs"]

    class B:
        name = "fake"
        def ask(self, prompt):
            return "policy-government, made-up-category"
    s = summarise.Summariser([B()])
    item2 = sources.Item(src, "Businessman launches national initiative", "https://e/2", None)
    assert summarise.pick_categories(s, item2, "A national programme was announced.", list(sources.CATEGORIES)) == ["policy-government"]
