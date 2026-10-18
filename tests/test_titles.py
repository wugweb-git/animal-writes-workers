from aw_workers import focus_keywords as fk
from aw_workers import ingest
from aw_workers.titles import no_hyphens


def test_hyphen_joining_words_becomes_a_space():
    assert no_hyphens("Human-Elephant Conflict Causes in Assam") == "Human Elephant Conflict Causes in Assam"
    assert no_hyphens("Centre of Excellence at WII-SACON, Coimbatore") == "Centre of Excellence at WII SACON, Coimbatore"
    assert no_hyphens("Victory: Bull-and-Horse Races") == "Victory: Bull and Horse Races"
    assert no_hyphens("Post- Exposure Prophylaxis") == "Post Exposure Prophylaxis"


def test_ranges_are_written_out_and_sentence_case_is_kept():
    assert no_hyphens("Reopens for 2026-27 Tourism Season") == "Reopens for 2026 to 2027 Tourism Season"
    assert no_hyphens("Research (1989-2025)") == "Research (1989 to 2025)"
    assert no_hyphens("DMC to resume monkey-catching drive") == "DMC to resume monkey catching drive"
    assert no_hyphens("Delhi–Dehradun Expressway") == "Delhi Dehradun Expressway"


def test_nothing_to_change_is_untouched():
    assert no_hyphens("Animal Writes – Animal welfare news") == "Animal Writes – Animal welfare news"


def test_keyword_never_has_a_hyphen():
    assert fk.clean("anti-rabies vaccine", "Anti Rabies Vaccine Drive", "") == "anti rabies vaccine"
    assert fk._normalise("Human-Elephant conflict") == "human elephant conflict"


def test_payload_title_has_no_hyphen():
    from aw_workers import sources

    src = sources.Source(id="x", name="X", kind="rss", type="news", url="https://x/feed")
    item = sources.Item(source=src, title="Man Pushes Cat Off 12th-floor Ledge", url="https://x/y", date=None)
    assert ingest.build_payload(item, "Summary.", categories=[])["title"] == "Man Pushes Cat Off 12th Floor Ledge"


def test_publisher_suffix_is_removed():
    from aw_workers.titles import no_publisher
    assert no_publisher("Two female leopards released into the wild after immunocontraceptive treatment | Hindustan Times", "Hindustan Times") == "Two female leopards released into the wild after immunocontraceptive treatment"
    assert no_publisher("Dogs rescued in Pune - The Hindu", "", "The Hindu feed", "www.thehindu.com") == "Dogs rescued in Pune"
    assert no_publisher("Rescue in Goa \u2014 Times of India | TOI", "Times of India", "TOI") == "Rescue in Goa"


def test_other_trailing_words_stay():
    from aw_workers.titles import no_publisher
    assert no_publisher("Stray dog orders: Delhi High Court", "LiveLaw") == "Stray dog orders: Delhi High Court"
    assert no_publisher("Cats and dogs - a guide", "The Hindu") == "Cats and dogs - a guide"
    assert no_publisher("Plain title", "") == "Plain title"
