"""Same event, one story (owner, 15 Oct 2026): a later report's facts and credit go to the first story."""

import json

from aw_workers import merge, relevance
from aw_workers.relevance import Candidate, Gate

REPORT_TITLE = "Pune: 14 dogs rescued from illegal breeder in Kondhwa"
REPORT_TEXT = ("Pune police on Monday rescued 14 dogs from an illegal breeding unit in Kondhwa. The Pune Municipal "
               "Corporation sealed the premises and the owner, Rakesh Jadhav, was booked under the Prevention of "
               "Cruelty to Animals Act.")
STORY = {"id": 501, "title": "Dogs rescued from illegal breeder in Pune",
         "summary": "Police rescued 14 dogs from an illegal breeding unit in Pune on Monday. The dogs were taken to a shelter.",
         "source_url": "https://first.example/a", "also": [], "additions": []}


class Backend:
    name, model = "fake", "m"

    def __init__(self, replies):
        self.replies, self.prompts = list(replies), []

    def ask(self, prompt):
        self.prompts.append(prompt)
        return self.replies.pop(0)


class Summ:
    def __init__(self, *backends):
        self.backends, self.dead = list(backends), set()


class Site:
    def __init__(self, story=None):
        self._story, self.sent = dict(story or STORY), []

    def story(self, story_id):
        assert story_id == self._story["id"]
        return self._story

    def same_story(self, body):
        self.sent.append(body)
        return {"credited": True, "added": bool(body["added_text"])}


REWRITE = ("Police rescued 14 dogs from an illegal breeding unit in Kondhwa, Pune, on Monday and took them to a shelter. "
           "The owner, Rakesh Jadhav, was booked under the Prevention of Cruelty to Animals Act, and the Pune Municipal "
           "Corporation sealed the premises.")
FACTS = "The owner, Rakesh Jadhav, was booked and the Pune Municipal Corporation sealed the premises in Kondhwa."


def adds(text, summary=""):
    return json.dumps({"adds": text, "summary": summary})


def fold(backend, site=None, **kw):
    site = site or Site()
    body = merge.fold(Summ(backend), site, 501, title=REPORT_TITLE, url="https://second.example/b",
                      publisher="The Second Paper", text=REPORT_TEXT, **kw)
    return body, site


def test_no_new_facts_gives_credit_only():
    body, site = fold(Backend([adds("")]))
    assert site.sent == [{"keep_id": 501, "source_url": "https://second.example/b", "publisher": "The Second Paper",
                          "added_text": "", "summary": "", "model": ""}]


def test_new_facts_rewrite_the_summary():
    body, site = fold(Backend([adds(FACTS, REWRITE)]))
    sent = site.sent[0]
    assert sent["summary"] == REWRITE and sent["added_text"] == FACTS and sent["model"] == "fake:m"
    assert body["summary_before"] == STORY["summary"] and "dup_id" not in sent


def test_invented_facts_never_reach_the_site():
    # A name and a number in neither report, twice: the summary stays as it is, only the credit is added.
    invented = REWRITE + " Officer Sunil Patil said 30 more dogs were found in Hadapsar."
    b = Backend([adds(FACTS, invented), adds(FACTS, invented)])
    body, site = fold(b)
    assert site.sent[0]["summary"] == "" and site.sent[0]["added_text"] == ""
    assert "broke these rules" in b.prompts[1] and "Sunil" in b.prompts[1] and "30" in b.prompts[1]


def test_stock_phrasing_and_lost_facts_are_refused():
    stock = "In a significant development, police rescued 14 dogs in Kondhwa, Pune. Furthermore, the owner was booked."
    assert any("stock phrases" in p for p in merge.check_summary(stock, STORY["summary"], REPORT_TITLE, REPORT_TEXT))
    lost = "Police rescued dogs from an illegal breeding unit in Kondhwa, Pune. The owner, Rakesh Jadhav, was booked."
    assert any("left out: 14" in p for p in merge.check_summary(lost, STORY["summary"], REPORT_TITLE, REPORT_TEXT))
    heading = "**Update**\nPolice rescued 14 dogs in Pune. The owner was booked."
    assert any("plain text" in p for p in merge.check_summary(heading, STORY["summary"], REPORT_TITLE, REPORT_TEXT))
    named = "Police rescued 14 dogs in Pune, The Second Paper said. The owner was booked on Monday."
    assert "names the publisher" in merge.check_summary(named, STORY["summary"], REPORT_TITLE, REPORT_TEXT, "The Second Paper")
    assert merge.check_summary(REWRITE, STORY["summary"], REPORT_TITLE, REPORT_TEXT, "The Second Paper") == []


def test_a_corrected_answer_is_used():
    b = Backend([adds(FACTS, "Furthermore, 14 dogs were rescued in Pune. The owner was booked."), adds(FACTS, REWRITE)])
    body, _ = fold(b)
    assert body["summary"] == REWRITE and "stock phrases: furthermore" in b.prompts[1]


def test_the_prompt_allows_only_the_two_reports_as_sources():
    b = Backend([adds("")])
    fold(b)
    p = b.prompts[0]
    assert "Use only facts written in OUR SUMMARY and in the NEW REPORT" in p and "Never add anything from memory" in p
    assert REPORT_TEXT in p and STORY["summary"] in p and "no heading" in p and "Vary sentence length" in p


def test_check_rules():
    assert merge.check("The owner, Rakesh Jadhav, was booked in Kondhwa.", REPORT_TITLE, REPORT_TEXT) == []
    assert merge.check("It happened on 12 March.", REPORT_TITLE, REPORT_TEXT)  # number and month not in the report
    assert merge.check("A. B. C. D.", "", "a b c d")  # four sentences
    assert merge.check("Police said 14 dogs were rescued.", REPORT_TITLE, REPORT_TEXT) == []  # opener + number from text


def test_a_report_already_folded_is_left_alone_unless_redone():
    story = dict(STORY, additions=[{"url": "https://second.example/b", "text": "x"}])
    b = Backend([])
    body, _ = fold(b, Site(story))
    assert b.prompts == [] and body["summary"] == ""
    body, _ = fold(Backend([adds(FACTS, REWRITE)]), Site(story), redo=True)
    assert body["summary"] == REWRITE


def test_dry_run_sends_nothing_and_duplicate_id_is_passed():
    site = Site()
    body = merge.fold(Summ(Backend([adds("")])), site, 501, title=REPORT_TITLE, url="u", publisher="P", text=REPORT_TEXT,
                      dry_run=True, dup_id=777)
    assert site.sent == [] and body["dup_id"] == 777


def test_no_text_or_no_provider_is_credit_only():
    body = merge.fold(Summ(), Site(), 501, title=REPORT_TITLE, url="u", publisher="P", text=REPORT_TEXT)
    assert body["summary"] == ""
    b = Backend([])
    body = merge.fold(Summ(b), Site(), 501, title=REPORT_TITLE, url="u", publisher="P", text="")
    assert body["summary"] == "" and b.prompts == []


def test_site_failure_never_raises():
    class Broken(Site):
        def story(self, story_id):
            raise RuntimeError("HTTP 500")

    assert merge.fold(Summ(Backend([])), Broken(), 501, title="t", url="u", publisher="p", text="x") is None


def test_gate_knows_the_first_story_id_from_the_site():
    class Cfg:
        def relevance_config(self):
            return {"categories": {}, "recent": ["Dogs rescued from illegal breeder in Pune"],
                    "recent_items": [{"id": 501, "title": "Dogs rescued from illegal breeder in Pune"}]}

    g = Gate.from_site(None, Cfg(), cache_file=relevance.Path("/nonexistent/cache.json"))
    assert g.kept_id("Dogs rescued from illegal breeder in Pune") == 501 and g.kept_id("other") == 0


def test_run_folds_a_same_event_report_into_the_first_story(fixture_text):
    # The gate says "same story as" a recent story: run.py sends the report's own text to the merge with that story's
    # ID, and the site receives the credit and the facts.
    from datetime import date

    from aw_workers import run
    from aw_workers.relevance import Verdict
    from tests.test_run import all_sources, fake_fetch

    class SameGate:
        recent_ids = {}

        def admit(self, c):
            return "reject", Verdict(relevant=True, category="", india=True, issuer="", reason="", model="m", at="",
                                     same_as=STORY["title"])

        def kept_id(self, title):
            return 501 if title == STORY["title"] else 0

    class Client(Site):
        def post(self, payload):
            raise AssertionError("a same-event report is not created")

    backend = Backend([adds("")] * 500)
    client = Client()
    src, defaults = all_sources()
    stats = run.run(src, {**defaults, "lookback_days": 3650}, dry_run=False, summariser=Summ(backend), client=client,
                    fetch=fake_fetch(fixture_text), today=date(2026, 10, 5), gate=SameGate())
    assert stats.rejected > 0 and len(client.sent) == stats.rejected
    assert all(b["keep_id"] == 501 and b["summary"] == "" for b in client.sent)
    assert backend.prompts and all("OUR SUMMARY" in p and STORY["summary"] in p for p in backend.prompts)


def test_page_text_reads_description_and_paragraphs():
    class Resp:
        text = ('<html><head><meta property="og:description" content="Fourteen dogs were rescued in Kondhwa."></head>'
                '<body><nav><p>Menu menu menu menu menu menu menu menu menu menu</p></nav><article><p>Pune police on Monday '
                'rescued 14 dogs from an illegal breeding unit in Kondhwa.</p><p>Short.</p></article></body></html>')

        def raise_for_status(self):
            pass

    class S:
        def get(self, url, **kw):
            return Resp()

    t = merge.page_text("https://x", session=S())
    assert t.startswith("Fourteen dogs were rescued in Kondhwa.") and "illegal breeding unit" in t
    assert "Menu" not in t and "Short." not in t


def test_fold_existing_pairs():
    from aw_workers import fold_existing

    assert fold_existing.pairs("9637:146, 10673:10665") == [(9637, 146), (10673, 10665)]
