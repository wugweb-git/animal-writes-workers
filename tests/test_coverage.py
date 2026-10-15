from datetime import date

from aw_workers import coverage, run

FEED = """<?xml version="1.0"?><rss><channel>
<item><title>Leopard rescued from well in Junnar, Pune - Times of India</title><link>https://news.google.com/rss/articles/A1</link><source url="https://timesofindia.indiatimes.com">Times of India</source></item>
<item><title>Leopard rescued from well in Junnar village - The Hindu</title><link>https://news.google.com/rss/articles/A2</link><source url="https://thehindu.com">The Hindu</source></item>
<item><title>Junnar leopard pulled out of well by forest team - Lokmat</title><link>https://news.google.com/rss/articles/A3</link><source url="https://lokmat.com">Lokmat</source></item>
<item><title>Leopard well rescue Junnar Pune forest - Hindustan Times</title><link>https://news.google.com/rss/articles/A4</link><source url="https://hindustantimes.com">Hindustan Times</source></item>
<item><title>Tiger census results announced in Delhi - NDTV</title><link>https://news.google.com/rss/articles/A5</link><source url="https://ndtv.com">NDTV</source></item>
</channel></rss>"""
TITLE = "Leopard rescued from well in Junnar, Pune"
LONG = "The forest department rescued a leopard from a well in Junnar taluka of Pune district. " * 4
HOSTS = {"A1": "https://timesofindia.indiatimes.com/a", "A2": "https://www.thehindu.com/b", "A3": "https://lokmat.com/c",
         "A4": "https://www.hindustantimes.com/d", "A5": "https://ndtv.com/e"}


def resolve(link):
    return HOSTS[link.rsplit("/", 1)[1]]


def test_key_words_put_names_and_places_first():
    assert coverage.key_words(TITLE)[:3] == ["Junnar", "Pune", "Leopard"] or coverage.key_words(TITLE)[:2] == ["Junnar", "Pune"]
    assert "the" not in [w.lower() for w in coverage.key_words("The leopard in the well")]


def test_search_url_has_the_key_names_and_a_date_window():
    url = coverage.search_url(TITLE, date(2026, 10, 10))
    assert url.startswith("https://news.google.com/rss/search?") and "Junnar" in url
    assert "after%3A2026-10-06" in url and "before%3A2026-10-15" in url and "gl=IN" in url


def test_parse_feed_strips_the_publisher_from_the_headline():
    rows = coverage.parse_feed(FEED)
    assert rows[0] == ("Leopard rescued from well in Junnar, Pune", "https://news.google.com/rss/articles/A1", "Times of India")
    assert coverage.parse_feed("not xml") == []


def test_same_event_needs_most_key_words():
    assert coverage.same_event(TITLE, "Leopard rescued from well in Junnar village")
    assert not coverage.same_event(TITLE, "Tiger census results announced in Delhi")


def test_find_skips_our_publisher_unreadable_pages_and_other_events():
    read = {"https://www.thehindu.com/b": LONG, "https://lokmat.com/c": "", "https://www.hindustantimes.com/d": LONG,
            "https://ndtv.com/e": LONG}
    got = coverage.find(TITLE, "https://timesofindia.indiatimes.com/own", date(2026, 10, 10), "Times of India",
                        get_feed=lambda u: FEED, resolve=resolve, read=lambda u: read.get(u, ""))
    assert [r.publisher for r in got] == ["The Hindu", "Hindustan Times"]  # Lokmat refused us: skipped, never forced
    assert all(r.text == LONG for r in got)


def test_find_never_raises_when_the_search_fails():
    def down(u):
        raise OSError("blocked")

    assert coverage.find(TITLE, "https://x.example/a", None, get_feed=down) == []


def test_combined_text_marks_each_other_report():
    r = coverage.Report("The Hindu", "https://www.thehindu.com/b", "Leopard rescued", LONG)
    text = coverage.combined_text("Own words.", [r])
    assert text.startswith("Own words.") and "Another report of the same event (The Hindu): Leopard rescued." in text


def test_credit_coverage_adds_logos_only():
    class Client:
        def __init__(self):
            self.bodies = []

        def same_story(self, body):
            self.bodies.append(body)
            return {}

    c = Client()
    r = coverage.Report("The Hindu", "https://www.thehindu.com/b", "Leopard rescued", LONG)
    assert run.credit_coverage(c, 42, [r]) == 1
    assert c.bodies == [{"keep_id": 42, "source_url": "https://www.thehindu.com/b", "publisher": "The Hindu",
                         "added_text": "", "summary": "", "model": ""}]
    assert run.credit_coverage(c, 42, []) == 0
