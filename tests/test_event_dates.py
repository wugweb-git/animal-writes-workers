"""Publication dates given as event dates (owner, 15 Oct 2026): the summariser, the same-story merge and the backfill."""

import json
from datetime import date

from aw_workers import event_dates as ed
from aw_workers import merge, summarise, summary_dates
from aw_workers.sources import Item, Source

PUB = date(2025, 12, 8)
AMBANI = ("On December 8, 2025, Anant Ambani received the Global Humanitarian Award for Animal Welfare from the Global "
          "Humane Society in Washington, DC. He was recognised for Vantara, a rescue centre in Jamnagar.")
NEWS = Source("n", "News", "rss", "news", "https://x")


# --- finding dates ------------------------------------------------------------------------------------------------

def test_find_dates_in_the_usual_forms():
    texts = ["On December 8, 2025, X", "on 8 December 2025 X", "on 8th of Dec. 2025", "Monday, 8 December, X",
             "dated 2025-12-08", "order of 08/12/2025", "on Dec 8 X"]
    for t in texts:
        found = ed.find_dates(t)
        assert len(found) == 1 and found[0].same_day(PUB), t
    assert ed.find_dates("in December 2025") == []  # a month is not a day
    assert ed.find_dates("officers may 8 times") == []  # "may" the verb


def test_the_publication_date_is_flagged_unless_the_source_gives_it_as_the_event_date():
    report = "Anant Ambani has received the Global Humanitarian Award from the Global Humane Society."
    assert [m.text for m in ed.pub_date_mentions(AMBANI, [PUB], report)] == ["December 8, 2025"]
    assert ed.pub_date_mentions(AMBANI, [PUB], "The award was presented on December 8 in Washington.") == []
    assert ed.pub_date_mentions(AMBANI, [PUB], "The Society today presented the award in Washington.") == []
    assert ed.pub_date_mentions(AMBANI, [PUB], "The award was presented on Monday in Washington.") == []  # 8 Dec 2025: Monday
    assert ed.pub_date_mentions(AMBANI, [date(2025, 12, 9)], report) == []  # not the publication date: not ours to judge


def test_datelines_bylines_quotes_and_ranges_are_not_event_dates():
    for source in ("NEW DELHI, December 8: Anant Ambani received an award.",
                   "Updated: December 8, 2025 10:30 IST. Anant Ambani received an award.",
                   "By Staff Reporter December 8, 2025 Anant Ambani received an award.",
                   "Mumbai, December 8, 2025 The award was given in Washington.",
                   "— Bhupender Yadav (@byadavbjp) December 8, 2025 The award",
                   "The week runs from 2nd - 8th December 2025. Anant Ambani received an award.",
                   '"We must protect animals today," he said. Anant Ambani received an award.',
                   "Today, caring for animals is a duty. Anant Ambani received an award.",
                   "Farmers are today compelled to sell. Anant Ambani received an award."):
        assert ed.pub_date_mentions(AMBANI, [PUB], source), source


# --- taking the date out ------------------------------------------------------------------------------------------

def _out(summary, pub=PUB, source="Nothing about the day."):
    return ed.remove_dates(summary, ed.pub_date_mentions(summary, [pub], source))


def test_date_phrases_come_out_and_every_other_word_stays():
    assert _out(AMBANI).startswith("Anant Ambani received the Global Humanitarian Award")
    cases = {
        "The court ordered a survey. The incident occurred on December 8, 2025.": "The court ordered a survey.",
        "Police booked the man. This decision was made on December 8, 2025. The dog is safe.":
            "Police booked the man. The dog is safe.",
        "A cow was rescued on 8 December 2025 in Pune by volunteers.": "A cow was rescued in Pune by volunteers.",
        "The cat fell. The event occurred on December 8, 2025, in India.": "The cat fell. The event occurred in India.",
        "An article published on 8 December 2025 examines street dogs.": "An article examines street dogs.",
        "Released on 8 December 2025, the survey showed concern for animals.": "The survey showed concern for animals.",
        "The event took place in Kerala and was reported on 8 December 2025.": "The event took place in Kerala.",
        "The concerns were raised on December 8, 2025. Activists asked for action.": "Activists asked for action.",
    }
    for before, after in cases.items():
        got = _out(before)
        assert got == after, before
        assert ed.clean_removal(before, got)


def test_a_date_that_cannot_come_out_cleanly_is_left():
    assert _out("A leopard entered a courtyard during the night of December 8, 2025.") is None
    assert not ed.clean_removal("A b c.", "a b c.")  # a sentence must start properly
    assert not ed.clean_removal("A b c.", "A b c d.")  # nothing may be added


# --- the summariser -----------------------------------------------------------------------------------------------

class Backend:
    name = "fake"

    def __init__(self, replies):
        self.replies, self.prompts = list(replies), []

    def ask(self, prompt):
        self.prompts.append(prompt)
        return self.replies.pop(0)


def _item(text="Anant Ambani has received the Global Humanitarian Award for Animal Welfare in Washington."):
    return Item(NEWS, "Anant Ambani receives global award for Vantara", "https://x/1", PUB, text)


def test_the_summary_prompt_forbids_the_publication_date():
    b = Backend(["Anant Ambani received an animal welfare award in Washington. It recognised his Vantara centre."])
    summarise.Summariser([b]).summarise(_item())
    assert "Never give the publication date below as the date of the\n  event" in b.prompts[0]
    assert "Published: 2025-12-08 (the day the report came out, not the date of the event)" in b.prompts[0]


def test_a_publication_date_gets_one_retry_then_is_taken_out():
    good = "Anant Ambani received an animal welfare award in Washington. It recognised his Vantara centre."
    b = Backend([AMBANI, good])
    assert summarise.Summariser([b]).summarise(_item()) == good
    assert "publication date as the event date" in b.prompts[1]
    b = Backend([AMBANI, AMBANI])  # the retry still gives it: the date phrase goes
    out = summarise.Summariser([b]).summarise(_item())
    assert out.startswith("Anant Ambani received the Global Humanitarian Award") and "2025" not in out


def test_a_date_the_source_gives_stays():
    b = Backend([AMBANI])
    out = summarise.Summariser([b]).summarise(_item("The award was presented on December 8 in Washington, DC."))
    assert out == AMBANI and len(b.prompts) == 1


# --- the same-story merge ------------------------------------------------------------------------------------------

OURS = "On December 8, 2025, police rescued 14 dogs from a breeder in Pune. The dogs went to a shelter."
NEW_TITLE = "Pune breeder booked after 14 dogs rescued"
NEW_TEXT = "Police rescued 14 dogs from an illegal breeder in Kondhwa, Pune. The owner, Rakesh Jadhav, was booked."
REWRITE = ("Police rescued 14 dogs from an illegal breeder in Kondhwa, Pune, and took them to a shelter. The owner, "
           "Rakesh Jadhav, was booked.")


def test_merge_does_not_protect_a_publication_date_in_our_summary():
    assert merge.check_summary(REWRITE, OURS, NEW_TITLE, NEW_TEXT, pub_dates=[PUB]) == []
    # without knowing the publication date, the old rule still keeps every number
    assert any("left out: 2025, 8" in p for p in merge.check_summary(REWRITE, OURS, NEW_TITLE, NEW_TEXT))
    # other numbers of ours are still protected
    lost = "Police rescued dogs from an illegal breeder in Kondhwa, Pune. The owner, Rakesh Jadhav, was booked."
    assert any("left out: 14" in p for p in merge.check_summary(lost, OURS, NEW_TITLE, NEW_TEXT, pub_dates=[PUB]))


def test_merge_refuses_a_publication_date_as_the_event_date():
    kept = "On December 8, 2025, " + REWRITE[0].lower() + REWRITE[1:]
    assert any("publication date as the event date" in p
               for p in merge.check_summary(kept, OURS, NEW_TITLE, NEW_TEXT, pub_dates=[PUB]))
    said = NEW_TEXT + " The raid took place on December 8."
    assert not any("publication date" in p for p in merge.check_summary(kept, OURS, NEW_TITLE, said, pub_dates=[PUB]))


def test_fold_passes_the_publication_dates_and_the_prompt_names_them():
    class Site:
        sent = []

        def story(self, i):
            return {"id": i, "summary": OURS, "source_date": "2025-12-08", "date": "2025-12-08", "additions": []}

        def same_story(self, body):
            self.sent.append(body)

    class Summ:
        dead = set()

        def __init__(self, b):
            self.backends = [b]

    b = Backend([json.dumps({"adds": "The owner, Rakesh Jadhav, was booked in Kondhwa.", "summary": REWRITE})])
    site = Site()
    body = merge.fold(Summ(b), site, 7, title=NEW_TITLE, url="https://y/2", publisher="Y", text=NEW_TEXT,
                      date=date(2025, 12, 9))
    assert body["summary"] == REWRITE and site.sent
    assert "8 December 2025" in b.prompts[0] and "9 December 2025" in b.prompts[0]
    assert "OUR SUMMARY may wrongly give such a publication date" in b.prompts[0]


# --- the backfill --------------------------------------------------------------------------------------------------

def _row(excerpt=AMBANI, **kw):
    return dict({"id": 11996, "type": "aw_news", "excerpt": excerpt, "source_date": "2025-12-08",
                 "post_date": "2025-12-08 09:00:00", "source_url": "https://news.example/a", "title": "Award"}, **kw)


def test_backfill_decisions():
    body = "Anant Ambani has received the Global Humanitarian Award for Animal Welfare. " * 6
    fix = summary_dates.decide(_row(), {"text": body, "published": ["2025-12-08"], "status": 200})
    assert fix["action"] == "fix" and fix["after"].startswith("Anant Ambani received") and fix["removed"] == ["December 8, 2025"]
    kept = summary_dates.decide(_row(), {"text": body + " It was presented on December 8.", "published": [], "status": 200})
    assert kept["action"] == "kept"
    left = summary_dates.decide(_row(), {"text": "", "published": [], "status": 403})
    assert left["action"] == "left" and "not readable (403)" in left["reason"]
    pdf = summary_dates.decide(_row(type="aw_legal"), {"text": "", "published": [], "status": "pdf"})
    assert pdf["action"] == "kept" and "Library document" in pdf["reason"]
    # the page's own published date counts as a publication date too
    other = summary_dates.decide(_row(source_date="2025-12-07", post_date="2025-12-07 09:00:00"),
                                 {"text": body, "published": ["2025-12-08"], "status": 200})
    assert other["action"] == "fix"
    assert summary_dates.decide(_row("No date here at all, only facts."), None)["action"] == "none"


def test_article_body_is_read_from_json_ld():
    raw = '<script type="application/ld+json">{"articleBody":"Police rescued 14 dogs on Monday.\\nThe owner was booked."}</script>'
    assert summary_dates._article_body(raw) == "Police rescued 14 dogs on Monday. The owner was booked."
