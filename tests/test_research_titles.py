from aw_workers import research_titles as rt

TEXT = "Ward-level data from Mumbai and Bengaluru show stray dog density is linked to 4.2 percent lower house prices."


def good():
    return rt.tidy({"headline": "Stray dogs in Indian cities linked to lower house prices, study shows",
                    "keyphrase": "stray dogs", "seo_title": "Stray Dogs and House Prices in India Study",
                    "slug": "stray-dogs-indian-cities-house-prices"})


def test_a_good_answer_passes_and_gets_the_brand_suffix():
    t = good()
    assert t["seo_title"] == "Stray Dogs and House Prices in India Study – Animal Writes"
    assert rt.check(t, TEXT) == ""


def test_hyphens_joining_words_go():
    t = rt.tidy({"headline": "Human-elephant conflict in Assam, study finds", "keyphrase": "human-elephant conflict",
                 "seo_title": "Human-Elephant Conflict in Assam Study", "slug": "human-elephant-conflict-assam"})
    assert t["headline"] == "Human elephant conflict in Assam, study finds"
    assert t["keyphrase"] == "human elephant conflict"
    assert rt.check(t, "Assam") == ""


def test_rules():
    t = good()
    assert "70" in rt.check(dict(t, headline="x " * 40 + "study"), TEXT)
    assert "study" in rt.check(dict(t, headline="Stray dogs in Indian cities linked to lower house prices"), TEXT)
    assert "methodology" in rt.check(dict(t, headline="Stray dogs and house prices, a hedonic study"), TEXT)
    assert "numbers" in rt.check(dict(t, headline="Stray dogs cut house prices by 9 percent, study shows"), TEXT)
    assert rt.check(dict(t, headline="Stray dogs cut house prices by 4.2 percent, study shows", slug="stray-dogs-house-prices"), TEXT) == ""
    assert "keyphrase" in rt.check(dict(t, keyphrase="dog density"), TEXT)
    assert "separator" in rt.check(dict(t, seo_title="Stray Dogs: House Prices – Animal Writes"), TEXT)
    assert "slug has words" in rt.check(dict(t, slug="stray-dogs-property"), TEXT)


def test_slug_from_headline_keeps_keyphrase_first_and_fits():
    s = rt.slug_from("Dog bites in Bidar, Karnataka, a study of injuries and treatment", "dog bites")
    assert s == "dog-bites-bidar-karnataka-study-injuries-treatment"
    assert len(rt.slug_from("word " * 40, "")) <= 60
