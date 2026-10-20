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
