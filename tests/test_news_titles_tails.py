"""News title tails, cut-off headlines and the not-News gate (owner, 20 Oct 2026)."""

from aw_workers import enrich
from aw_workers.sources import not_an_article
from aw_workers.titles import completes, is_cut, no_section


def test_section_labels_are_removed():
    assert no_section("Corbett’s 80 year old elephant Sonakali dies | India News", "Hindustan Times") == "Corbett’s 80 year old elephant Sonakali dies"
    assert no_section("Dog beaten with iron rod in Uttar Pradesh | Watch", "NewsX") == "Dog beaten with iron rod in Uttar Pradesh"
    assert no_section("Goat rescued from circus in Dimapur - Blog", "PETA India") == "Goat rescued from circus in Dimapur"
    assert no_section("Bulls forced to haul carts - blog", "PETA India") == "Bulls forced to haul carts"
    assert no_section("Animal feeding in IIT B restricted to 19 spots | Mumbai news", "Hindustan Times") == "Animal feeding in IIT B restricted to 19 spots"
    assert no_section("Maid kills puppy in lift | Trending | Hindustan Times", "Hindustan Times") == "Maid kills puppy in lift"
    assert no_section("SC directions on dog bites | SCC Times", "SCC Online") == "SC directions on dog bites"
    assert no_section("Tiger reintroduction in Sariska | Akashvani News", "News On AIR") == "Tiger reintroduction in Sariska"
    assert no_section("Shiv Sena leader dumps puppies on desk; booked | VIDEO", "Mathrubhumi") == "Shiv Sena leader dumps puppies on desk; booked"


def test_publisher_department_tail_after_a_pipe():
    t = "SAWAB N holds 7th board meeting | DIPR Nagaland Department of Information & Public Relations, Nagaland"
    assert no_section(t, "DIPR Nagaland") == "SAWAB N holds 7th board meeting"


def test_headline_parts_stay():
    for t in ("‘Tiger’, ‘Zebra’ and ‘Giraffe’ make plea: Save our habitat – go vegan",
              "From farm to fork: The hidden lives of animals | World Farm Animals Day 2026",
              "Bulls forced to haul carts for Pepsi sugar — PETA India campaign targets Gen Z",
              "Interview | ‘Tiger man’ K. Ullas Karanth on protecting habitats",
              "Watchman bitten by stray dog in Pune"):
        assert no_section(t, "PETA India", "WION", "The Hindu") == t


def test_cut_headline_is_recognised_and_completed():
    cut = "Bombay High Court threatens contempt action against Goa civic officials over failure to submit stray dog, ..."
    assert is_cut(cut) and is_cut("Centre proposes doubling bank guarantee for vet…") and not is_cut("Dogs rescued")
    full = "Bombay High Court Threatens Contempt Action Against Goa Civic Officials Over Failure To Submit Stray Dog, Cattle Data"
    assert completes(cut, full)
    assert completes("Ken Betwa Project: MP High Court seeks Centre, state replies on PIL raising wildlife, environmental...",
                     "Ken-Betwa Project: MP High Court Seeks Centre, State Replies On PIL Raising Wildlife, Environmental Concerns")
    assert completes("Why are Indian cities seeing more human wildlife encounters? Shrinking forests and broken corridors offer c...",
                     "Why are Indian cities seeing more human-wildlife encounters? Shrinking forests and broken corridors offer clues")
    assert not completes(cut, "Allahabad HC Directs Repair Of Lucknow Crematorium")  # a different headline
    assert not completes("Centre proposes doubling bank guarantee for vet…", "Centre proposes doubling bank guarantee for vet… | UNI India")


class _Resp:
    def __init__(self, text):
        self.text = text


class _Session:
    def __init__(self, page):
        self.page = page

    def get(self, url, headers=None, timeout=None):
        return _Resp(self.page)


def test_full_headline_order_og_title_then_title_then_h1():
    cut = "Centre proposes doubling bank guarantee for vet…"
    page = ('<meta property="og:title" content="Centre proposes doubling bank guarantee for vet… | UNI India">'
            "<title>Centre proposes doubling bank guarantee for vet… | UNI India</title>"
            "<h1 class=x>Centre proposes doubling bank guarantee for veterinary colleges to Rs 10 crore</h1>")
    assert enrich.full_headline("https://u.example/a", cut, _Session(page)) == "Centre proposes doubling bank guarantee for veterinary colleges to Rs 10 crore"
    page = """<meta property="og:title" content="Karnataka High Court Closes DRDO Officers' Quash Plea In Case Over 21 Stray Dogs">"""
    assert enrich.full_headline("https://u.example/b", "Karnataka High Court closes DRDO officers' quash plea in case...", _Session(page)) == \
        "Karnataka High Court Closes DRDO Officers' Quash Plea In Case Over 21 Stray Dogs"
    assert enrich.full_headline("https://u.example/c", cut, _Session("<title>Attention Required! | Cloudflare</title>")) == ""


def test_archive_and_tag_pages_are_not_news():
    assert not_an_article("https://www.livelaw.in/x", "Read all latest updates on and about Tamil Nadu Forest And Wildlife Crime Control Bureau")
    assert not_an_article("https://example.in/x", "Latest news on Project Cheetah")
    assert not_an_article("https://example.in/x", "Latest News, Photos, Videos on Stray Dogs")
    assert not_an_article("https://example.in/x", "Latest on Project Cheetah: two cubs born") == ""


def test_job_vacancies_are_not_news_but_pass_from_opportunity_sources():
    t = "Legal officer (young professional II) vacancy at Tamil Nadu Forest And Wildlife Crime Control Bureau"
    url = "https://www.livelaw.in/job-updates/legal-officer-vacancy-549908"
    assert "job vacancy" in not_an_article(url, t)
    assert not_an_article("https://www.freejobalert.com/articles/awbi-legal-advisor-recruitment-2026", "AWBI legal advisor recruitment 2026 - apply offline")
    assert not_an_article("https://example.in/x", "Wildlife SOS is hiring veterinary officers")
    assert not_an_article(url, t, "opportunity") == ""
    for news in ("Amid poaching concerns, apex tiger body flags 63.81% vacancies in Bandipur Tiger Reserve",
                 "Forest guard recruitment scam: three held", "Veterinary college students protest fee hike"):
        assert not_an_article("https://example.in/news/x", news) == ""
