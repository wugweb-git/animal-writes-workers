import json

from aw_workers import headline_case as hc
from aw_workers import summarise


class _Backend:
    name = "fake"
    api_key = "k"

    def __init__(self, reply):
        self.reply = reply
        self.calls = 0

    def ask(self, prompt):
        self.calls += 1
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def _owner(*answers):
    return summarise.Summariser([_Backend("\n".join(json.dumps({"id": i, "title": t}) for i, t in answers))])


TC = "Yogi Government Links Gaushalas With Natural Farming to Boost Rural Economy"
SC = "Yogi government links gaushalas with natural farming to boost rural economy"


def test_case_only_is_string_equality_ignoring_case():
    assert hc.case_only(TC, SC)
    assert not hc.case_only(TC, SC.replace("boost", "lift"))
    assert not hc.case_only(TC, SC + ".")
    assert not hc.case_only(TC, "")


def test_title_case_is_cased_and_proper_nouns_are_kept():
    before = "Hi Tech Gaushala Project Delay Proves Costly For Sanskriti Jain, Bhopal BMC Commissioner Transferred"
    after = "Hi tech gaushala project delay proves costly for Sanskriti Jain, Bhopal BMC commissioner transferred"
    assert hc.case_title(_owner((1, after)), before) == (after, "")


def test_a_changed_word_keeps_the_original_and_flags_it():
    assert hc.case_title(_owner((1, SC.replace("Yogi government", "UP government"))), TC) == (TC, "words changed")


def test_no_answer_keeps_the_original_and_flags_it():
    assert hc.case_title(summarise.Summariser([_Backend(Exception("429 rate limit"))]), TC)[1] in ("no answer",)


def test_sentence_case_titles_are_not_sent():
    owner = _owner((1, "x"))
    title = "Viral video of man biting goat to death sparks FIR in Telangana"
    assert hc.case_title(owner, title) == (title, "")
    assert owner.backends[0].calls == 0


def test_a_lower_case_first_letter_is_refused():
    assert hc.decide(TC, SC[0].lower() + SC[1:])[1] == "flag: first letter lower case"


def test_lead_ins_are_listed_not_removed():
    assert hc.lead_in("Watch: UP Man Caught On Camera") == ("Watch:", "UP Man Caught On Camera")
    assert hc.lead_in("VIDEO | Ghaziabad Dog Beaten") == ("VIDEO |", "Ghaziabad Dog Beaten")
    assert hc.lead_in("Video Of Helpless Monkeys Has Gone Viral") == ("Video Of", "")
    assert hc.lead_in("Viral video of man biting goat") == ("Viral video of", "")
    assert hc.lead_in("Videos show elephants crossing") == ("", "")


def test_dry_run_rows():
    rows = [{"id": 1, "title": TC}, {"id": 2, "title": "Watch: Stray Dogs Rescued From Drain In Pune"},
            {"id": 3, "title": "Leopard spotted in Nashik"}]
    owner = _owner((1, SC), (2, "Watch: stray dogs rescued from drain in Pune"))
    out = {r["id"]: r for r in hc.dry_run(owner, rows)}
    assert out[1]["after"] == SC and out[1]["status"] == "case changed"
    assert out[2]["lead_in"] == "Watch:" and out[2]["without_lead_in"] == "Stray dogs rescued from drain in Pune"
    assert 3 not in out


class _Resp:
    status_code = 200

    def json(self):
        return {"status": "created"}


class _Session:
    def __init__(self):
        self.sent = []
        self.auth = None

    def post(self, url, json=None, timeout=None):
        self.sent.append(json)
        return _Resp()


def test_press_titles_are_cased_on_the_way_in_and_news_is_left_to_run():
    from aw_workers import ingest

    s = _Session()
    c = ingest.Client("https://x.test", "u", "p", session=s)
    c.title_caser = lambda t: ("Animal Writes on dogs in Delhi", "") if t == "Animal Writes On Dogs In Delhi" else (t, "words changed")
    c.post({"type": "press", "title": "Animal Writes On Dogs In Delhi"})
    c.post({"type": "press", "title": "Other Title Here Now"})
    c.post({"type": "news", "title": "News Title Stays Here"})
    assert s.sent[0]["title"] == "Animal Writes on dogs in Delhi" and "title_flag" not in s.sent[0]
    assert s.sent[1]["title"] == "Other Title Here Now" and s.sent[1]["title_flag"] == "words changed"
    assert s.sent[2]["title"] == "News Title Stays Here"


def test_press_caser_needs_a_provider_key():
    assert hc.press_caser({}) is None


def test_a_refused_login_is_tried_once_more():
    """25 and 30 Oct 2026: the site refused the workers' login for one run; the next run worked."""
    from aw_workers import ingest

    class Flaky:
        def __init__(self, codes):
            self.codes, self.calls = list(codes), 0

        def request(self, method, url, **kw):
            self.calls += 1
            code = self.codes.pop(0)

            class R:
                status_code = code

                def raise_for_status(self):
                    pass

                def json(self):
                    return {"ok": True}

            return R()

    ok = ingest.Client("https://x.test", "u", "p", session=Flaky([403, 200]))
    assert ok._call("GET", "/search-insights") == {"ok": True} and ok.session.calls == 2
    bad = ingest.Client("https://x.test", "u", "p", session=Flaky([403, 403]))
    import pytest

    with pytest.raises(ingest.AuthError):
        bad._call("GET", "/search-insights")
    assert bad.session.calls == 2  # one retry only
