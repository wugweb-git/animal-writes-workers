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


class _Fake:
    name, model, api_key = "fake", "m", "k"

    def __init__(self, reply):
        self.reply = reply

    def ask(self, prompt):
        return self.reply


def test_research_worker_sends_headline_fields():
    import json

    from aw_workers import research, summarise

    reply = json.dumps([{"id": 1, "headline": "Stray dogs linked to lower house prices in Mumbai, study shows",
                         "keyphrase": "stray dogs", "seo_title": "Stray Dogs and House Prices in Mumbai Study",
                         "slug": "stray-dogs-house-prices-mumbai-study"}])
    owner = summarise.Summariser([_Fake(reply)])
    got = research.headline_fields(owner, {"title": "Stray dog density: a hedonic analysis", "url": "u",
                                           "abstract": "Mumbai house prices"}, "Stray dogs and house prices in Mumbai.")
    assert got == {"title": "Stray dogs linked to lower house prices in Mumbai, study shows",
                   "seo_title": "Stray Dogs and House Prices in Mumbai Study – Animal Writes",
                   "focus_keyword": "stray dogs", "slug": "stray-dogs-house-prices-mumbai-study"}
    bad = summarise.Summariser([_Fake(reply.replace("study shows", "report"))])
    assert research.headline_fields(bad, {"title": "x", "url": "u"}, "Mumbai") == {}


def test_india_must_be_confirmed_not_just_no_foreign_name():
    from types import SimpleNamespace

    from aw_workers.research import india_doubt

    yes, no = SimpleNamespace(india=True), SimpleNamespace(india=False)
    assert india_doubt({"title": "Dog bites in Bidar, Karnataka", "abstract": ""}) == ""
    assert india_doubt({"title": "Land-use change and human-elephant conflict in elephant conservation",
                        "abstract": "We reviewed 31 studies, mostly from India and China."}, yes) == ""
    assert "AI check" in india_doubt({"title": "Land-use change and human-elephant conflict in Borneo elephant conservation",
                                      "abstract": "31 studies, mostly from India and China."}, no)
    assert "not named" in india_doubt({"title": "Rabies in dogs: a review", "abstract": "A global review."}, yes)


def test_numbers_must_come_from_the_source():
    from aw_workers import summary_rules

    src = "The overall ARV completion rate (up to the 3rd dose) was 70.8%. Two hundred and one patients; 5% washed wounds."
    assert summary_rules.unsourced_numbers("Only 63.8 percent completed the vaccine.", src) == ["63.8"]
    assert summary_rules.unsourced_numbers("70.8 percent took three doses; 201 patients; 5 percent washed.", src) == []
    assert summary_rules.unsourced_numbers("In 2026, 70.8 percent.", src, {"2026"}) == []
