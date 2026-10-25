from datetime import date
from pathlib import Path

from aw_workers import sources
from aw_workers.sources import Source

TODAY = date(2026, 10, 5)


def src(kind, type_="news", keywords=None, url="https://example.com/"):
    return Source(id="t", name="Test", kind=kind, type=type_, url=url, keywords=keywords or [])


# sources.yaml is copied from the site; tests that need fixed sources read the copy of 25 Oct 2026 (before phase 2).
FROZEN = Path(__file__).parent / "fixtures" / "sources_frozen.yaml"


def test_sources_yaml_loads():
    loaded, defaults = sources.load_sources("sources.yaml")
    assert {s.type for s in loaded} == {"news", "legal", "opportunity"}
    assert {s.kind for s in loaded} <= set(sources.PARSERS) | set(sources.SELF_FETCH)
    assert defaults["lookback_days"] > 0


def test_google_news_rss(fixture_text):
    items = sources.parse_rss(src("rss"), fixture_text("google_news.xml"))
    assert len(items) > 10
    first = items[0]
    assert first.title and first.url.startswith("https://news.google.com/")
    assert isinstance(first.date, date)


def test_awbi_table(fixture_text):
    s = src("awbi", "legal", url="https://awbi.gov.in/circulars")
    items = sources.parse_awbi(s, fixture_text("awbi_circulars.html"))
    assert len(items) >= 4
    world_animal_day = next(i for i in items if "World Animal Day" in i.title)
    assert world_animal_day.date == date(2026, 10, 2)
    assert world_animal_day.url.startswith("https://awbi.gov.in/uploads/circular/")
    assert " " not in world_animal_day.url


def test_pib_real_feed_parses(fixture_text):
    items = sources.parse_rss(src("rss", "legal"), fixture_text("pib.xml"))
    assert len(items) == 20
    assert all(i.url.startswith("https://pib.gov.in/") for i in items)
    assert all(i.date is None for i in items)  # PIB gives no dates


def test_pib_keyword_filter_and_date_fallback(fixture_text):
    loaded, _ = sources.load_sources(FROZEN)
    pib = next(s for s in loaded if s.id == "pib-releases")
    items = sources.collect(pib, fixture_text("pib_sample.xml"), TODAY, 14, 15)
    assert [i.url[-7:] for i in items] == ["1000001", "1000002"]  # English + Hindi match, highway dropped
    assert all(i.date == TODAY for i in items)


def test_lookback_and_limit(fixture_text):
    s = src("awbi", "legal", url="https://awbi.gov.in/circulars")
    items = sources.collect(s, fixture_text("awbi_circulars.html"), TODAY, 30, 15)
    assert items and all((TODAY - i.date).days <= 30 for i in items)
    assert len(sources.collect(s, fixture_text("awbi_circulars.html"), TODAY, 3650, 2)) == 2


def test_awbi_undated_rows_use_upload_timestamp():
    html = (
        '<table><tr><td>Public Notice</td><td><a class="downBtn" title="Revised fee order" '
        'href="/uploads/notifications/163307778792revised fee order (1).pdf">Download</a></td></tr></table>'
    )
    s = src("awbi", "legal", url="https://awbi.gov.in/notifications")
    [item] = sources.parse_awbi(s, html)
    assert item.date == date(2021, 10, 1)
    assert sources.collect(s, html, TODAY, 14, 15) == []  # old notice is not "new"


def test_category_tagging():
    loaded, _ = sources.load_sources(FROZEN)
    src_ = loaded[0]
    item = sources.Item(src_, "High Court orders ABC programme for street dogs after rabies cases", "https://x", TODAY)
    tags = sources.categories_for(item)
    for t in ["community-dogs", "animal-birth-control", "rabies-public-health", "courts-judgments"]:
        assert t in tags
    assert "birds" not in tags and "wildlife" not in tags
    plain = sources.Item(src_, "Pune zoo welcomes visitors", "https://y", TODAY)
    assert "pets-breeding" not in sources.categories_for(plain)  # no false match on short keys


def test_short_keywords_match_whole_words_only():
    loaded, _ = sources.load_sources(FROZEN)
    item = sources.Item(loaded[0], "First action taken at the impact assessment", "https://z", TODAY)
    tags = sources.categories_for(item)
    assert "cruelty-prosecution" not in tags  # "fir" must not match "first"
    assert "laws-rules" not in tags           # "act" must not match "action"/"impact"


def test_google_news_publisher_split(fixture_text):
    items = sources.parse_rss(src("rss"), fixture_text("google_news.xml"))
    first = items[0]
    assert first.publisher == "BollySpice.com"
    assert not first.title.endswith("BollySpice.com") and first.title.startswith("World Animal Day")


def test_section_lists_limit_categories_and_tags():
    from aw_workers import ingest

    sources.CATEGORIES.clear()
    sources.CATEGORIES.update({"community-dogs": ["stray dog"], "laws-rules": ["rules"], "wildlife": ["leopard"]})
    sources.SECTIONS.clear()
    try:
        assert sources.allowed_categories("opportunity") == ["community-dogs", "laws-rules", "wildlife"]  # no lists yet: all
        assert sources.allowed_tags("opportunity") is None
        sources.set_sections({"aw_opportunity": {"categories": {"wildlife": "Wildlife", "community-dogs": "Community Dogs"},
                                                 "tags": {"job": "Job", "volunteer": "Volunteer"}}})
        assert sources.allowed_categories("opportunity") == ["wildlife", "community-dogs"]
        opp = Source(id="o", name="Org", kind="sjm", type="opportunity", url="https://example.com/", org="Org", opportunity_type="job")
        item = sources.Item(source=opp, title="Stray dog and leopard rules officer", url="https://example.com/1", date=TODAY)
        p = ingest.build_payload(item, "x")
        assert p["categories"] == ["community-dogs", "wildlife"]  # laws-rules is not an Opportunities category
        assert p["tags"] == ["job"] and "opportunity_type" not in p
        opp.opportunity_type = "grant"  # not in the Opportunities tags
        assert "tags" not in ingest.build_payload(item, "x")
    finally:
        sources.SECTIONS.clear()
