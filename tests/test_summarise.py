from datetime import date
from types import SimpleNamespace

import pytest

from aw_workers import summarise, summary_rules
from aw_workers.sources import Item, Source

NEWS = Source("n", "News", "rss", "news", "https://x")
LEGAL = Source("l", "AWBI", "awbi", "legal", "https://x")
SOURCE_TEXT = (
    "The district administration in Pune opened a new animal shelter on Monday that will house up to "
    "two hundred street dogs and provide free vaccination and sterilisation for community animals. The shelter in "
    "Hadapsar was built at a cost of Rs 2 crore and has a veterinary clinic, an isolation ward and space for "
    "adoption drives. Officials said volunteers from three local NGOs will help run the centre, and residents can "
    "report injured or sick animals on a helpline. The municipal corporation will review the shelter's work after "
    "six months, the collector said, and a second shelter is planned for Pimpri."
)


def item(src=NEWS, text=SOURCE_TEXT):
    return Item(src, "Pune opens new animal shelter", "https://x/1", date(2026, 10, 1), text)


# Follows the News rules: 45-100 words, 3-5 sentences, a short lead, the searched words (street dogs, NGOs).
GOOD = ("Pune's district administration has opened an animal shelter in Hadapsar for up to 200 street dogs. "
        "Built for Rs 2 crore, it has a veterinary clinic and an isolation ward, and offers free vaccination and "
        "sterilisation. Volunteers from three local NGOs will help run it, and the municipal corporation will review "
        "its work after six months.")
BAD = "Furthermore, " + GOOD  # stock wording: refused, not short
SHORT = "Pune has opened a shelter for 200 street dogs. It is in Hadapsar. Three NGOs help."


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0
        self.messages = self

    def create(self, **kwargs):
        assert kwargs["model"] == "claude-haiku-4-5"
        self.calls += 1
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.replies.pop(0))],
                               usage=SimpleNamespace(input_tokens=600, output_tokens=100))


def test_good_summary_passes():
    assert summarise.check_summary(GOOD, item()) == []


@pytest.mark.parametrize(
    "text,problem",
    [
        ("One sentence only.", "short: needs at least 45 words"),
        ("One. Two. Three. Four. Five. Six.", "at most 5 sentences"),
        (GOOD + " " + " ".join(["word"] * 50) + ".", "over 100 words"),
        (GOOD + ' Officials said "this is a very long quoted statement that goes on and on well past the twenty-five '
         'word limit we set for any quote in a summary".', "quote longer than 25"),
        (GOOD + " The rescue highlights the visionary work of the city.", "stock or praise phrases: highlights, visionary"),
        (GOOD + " Here is a summary of it.", "reads as generated text"),
        (GOOD + " यह आश्रय है.", "English only"),
        (GOOD + " Read more at https://x.example/a.", "plain text only"),
        (GOOD.replace("street dogs", "neighbourhood animals"), "keep the searched words"),
        (GOOD + " It follows the ABC Rules.", "write 'Animal Birth Control Rules, 2023', not 'ABC Rules'"),
    ],
)
def test_bad_summaries_fail(text, problem):
    assert any(problem in p for p in summarise.check_summary(text, item()))


def test_each_section_has_its_own_length():
    assert any("at least 60 words" in p for p in summarise.check_summary(GOOD, item(LEGAL)))  # Library: 60-130
    rules = summary_rules.merged({"sections": {"news": {"min_words": 20}}})  # the site's numbers win
    assert summarise.check_summary(GOOD, item(), rules) == []
    assert rules["sections"]["news"]["max_words"] == 100  # the rest stays as the defaults


def test_copied_run_detected():
    copied = GOOD + " The shelter in Hadapsar was built at a cost of Rs 2 crore and has a veterinary clinic, an isolation ward and space for adoption drives."
    assert any("consecutive words" in p for p in summarise.check_summary(copied, item()))


def test_exact_names_and_quotes_are_not_copying():
    name = "the Prevention of Cruelty to Animals (Dog Breeding and Marketing) Rules, 2017"
    src = SOURCE_TEXT + f" The breeder was booked under {name} and Section 11 of the Act."
    entities = summarise.summary_rules.DEFAULTS["entities"] + [name.replace("the ", "", 1)]
    rules = summary_rules.merged({"entities": entities})
    ok = GOOD + f" The breeder was booked under {name}."
    assert not any("consecutive" in p for p in summarise.check_summary(ok, item(text=src), rules))


def test_quote_of_fifteen_words_allowed():
    q = " ".join(["word"] * 15)
    assert summarise.longest_quote_words(f'He said "{q}". Then left.') == 15


def test_retry_then_success():
    client = FakeClient(["Too short.", GOOD])
    out = summarise.Summariser([summarise.ClaudeBackend("k", client=client)]).summarise(item())
    assert out == GOOD and client.calls == 2


def test_gives_up_after_retry():
    client = FakeClient([BAD, BAD])
    with pytest.raises(summarise.SummaryError):
        summarise.Summariser([summarise.ClaudeBackend("k", client=client)]).summarise(item())


def test_short_fallback_reads_the_page_again_then_publishes_the_short_summary():
    thin = item(text="Pune opened a shelter for 200 street dogs in Hadapsar.")
    pages, searched = [], []
    b = Fixed("haiku", SHORT)
    s = summarise.Summariser([b], fetch=lambda url: pages.append(url) or "",
                             coverage=lambda *a: searched.append(a) or [])
    thin.url = "https://news.example/pune-shelter"
    out = s.summarise(thin)
    assert out == SHORT and s.last_short and pages == [thin.url] and b.calls == 1  # never padded, no second ask
    assert searched and searched[0][0] == thin.title and not s.last_coverage  # other coverage was looked for
    b2 = Fixed("haiku", SHORT)
    b2.ask = lambda prompt, _b=b2: (setattr(_b, "calls", _b.calls + 1), GOOD if SOURCE_TEXT[:60] in prompt else SHORT)[1]
    s = summarise.Summariser([b2], fetch=lambda url: SOURCE_TEXT)
    assert s.summarise(thin) == GOOD and not s.last_short  # written again from the page


def test_prompt_is_built_from_the_rules():
    p = summarise.prompt_for(item(LEGAL, SOURCE_TEXT + " under the PCA Act."))
    assert "60 to 130 words" in p and "who it applies to" in p and "for its Library" in p
    assert "Name these exactly so: Prevention of Cruelty to Animals Act, 1960" in p
    rules = summary_rules.merged({"sections": {"news": {"structure": "Lead first, then the rest."}}})
    assert "Structure: Lead first, then the rest." in summarise.prompt_for(item(), rules)


def test_rules_are_read_from_the_site_and_fall_back_to_the_defaults():
    class Site:
        def summary_rules(self):
            return {"sections": {"news": {"min_words": "50"}}, "stock_phrases": ["iconic"]}

    class Down:
        def summary_rules(self):
            raise RuntimeError("HTTP 500")

    try:
        assert summary_rules.use(Site())["sections"]["news"]["min_words"] == 50
        assert summary_rules.current()["stock_phrases"] == ["iconic"]
        assert summary_rules.use(Down()) == summary_rules.merged(None)
    finally:
        summary_rules.reset()


def test_legal_note_added():
    legal_good = GOOD + " Feeders and residents can report injured animals on the helpline, officials said."
    out = summarise.Summariser([summarise.ClaudeBackend("k", client=FakeClient([legal_good]))]).summarise(item(LEGAL))
    assert out.endswith("\n[Needs legal review]")
    news = summarise.Summariser([summarise.ClaudeBackend("k", client=FakeClient([GOOD]))]).summarise(item())
    assert "legal review" not in news


class FakeResp:
    def __init__(self, data, code=200, headers=None):
        self._data, self.status_code, self.headers = data, code, headers or {}

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def post(self, url, headers, json, timeout):
        self.calls.append((url, headers, json))
        return self.responses.pop(0)


def test_gemini_request_and_parse():
    sess = FakeSession([FakeResp({"candidates": [{"content": {"parts": [
        {"text": "thinking...", "thought": True}, {"text": GOOD}]}}]})])
    b = summarise.GeminiBackend("gk", session=sess, sleep=lambda s: None)
    assert b.ask("p") == GOOD
    url, headers, body = sess.calls[0]
    assert url.endswith("/models/gemini-flash-latest:generateContent")
    assert headers == {"x-goog-api-key": "gk"} and body["contents"][0]["parts"][0]["text"] == "p"


def test_groq_request_and_parse():
    sess = FakeSession([FakeResp({"choices": [{"message": {"content": GOOD}}]})])
    b = summarise.GroqBackend("qk", session=sess, sleep=lambda s: None)
    assert b.ask("p") == GOOD
    url, headers, body = sess.calls[0]
    assert url == "https://api.groq.com/openai/v1/chat/completions"
    assert headers["Authorization"] == "Bearer qk" and body["model"] == "openai/gpt-oss-120b"


class Failing:
    name = "haiku"

    def ask(self, prompt):
        raise RuntimeError("overloaded")


class Fixed:
    def __init__(self, name, reply):
        self.name, self.reply, self.calls = name, reply, 0

    def ask(self, prompt):
        self.calls += 1
        return self.reply


def test_haiku_first_when_it_works():
    haiku, gemini = Fixed("haiku", GOOD), Fixed("gemini", GOOD)
    s = summarise.Summariser([haiku, gemini])
    s.summarise(item())
    assert (haiku.calls, gemini.calls) == (1, 0) and s.used == {"haiku": 1}


def test_falls_back_on_error_and_on_failed_checks():
    bad_quality = Fixed("gemini", BAD)
    groq = Fixed("groq", GOOD)
    s = summarise.Summariser([Failing(), bad_quality, groq])
    assert s.summarise(item()) == GOOD
    assert bad_quality.calls == 2 and s.used == {"groq": 1}


def test_all_providers_fail():
    with pytest.raises(summarise.SummaryError, match="haiku: overloaded"):
        summarise.Summariser([Failing(), Fixed("groq", BAD)]).summarise(item())


def test_backends_from_env_order_and_skips_missing_keys():
    env = {"GEMINI_API_KEY": "g", "GROQ_API_KEY": "q"}
    assert [b.name for b in summarise.backends_from_env(env)] == ["gemini", "groq"]
    env["ANTHROPIC_API_KEY"] = "a"
    assert [b.name for b in summarise.backends_from_env(env)] == ["haiku", "gemini", "groq"]
    env["LLM_PROVIDERS"] = "groq, haiku"
    assert [b.name for b in summarise.backends_from_env(env)] == ["groq", "haiku"]
    with pytest.raises(ValueError):
        summarise.backends_from_env({"LLM_PROVIDERS": "nosuchprovider"})
    env = {"DEEPSEEK_API_KEY": "d", "OPENAI_API_KEY": "o1\no2", "GEMINI_API_KEY": "g", "GEMINI_API_KEY_2": "g2",
           "LLM_PROVIDERS": "deepseek,openai,gemini"}
    assert [b.name for b in summarise.backends_from_env(env)] == ["deepseek", "openai", "openai", "gemini", "gemini"]


def test_haiku_budget_tracks_spend_and_stops():
    ledger = {}
    b = summarise.ClaudeBackend("k", client=FakeClient([GOOD, GOOD]), budget_usd=0.0015, ledger=ledger, month="2026-10")
    b.ask("p")
    assert ledger == {"2026-10": 0.0011}  # 600 in * $1/M + 100 out * $5/M
    b.ask("p")  # 0.0011 < 0.0015, still allowed
    with pytest.raises(summarise.BudgetExceeded):
        b.ask("p")  # 0.0022 >= cap


def test_over_budget_haiku_falls_back_to_free():
    ledger = {"2026-10": 4.0}
    haiku = summarise.ClaudeBackend("k", client=FakeClient([]), budget_usd=4, ledger=ledger, month="2026-10")
    gemini = Fixed("gemini", GOOD)
    s = summarise.Summariser([haiku, gemini])
    assert s.summarise(item()) == GOOD and s.used == {"gemini": 1}


def test_budget_default_is_four_dollars():
    [b] = summarise.backends_from_env({"ANTHROPIC_API_KEY": "a"})
    assert b.budget_usd == 4.0


def test_free_tier_retries_once_after_a_short_wait():
    ok = FakeResp({"candidates": [{"content": {"parts": [{"text": GOOD}]}}]})
    sess = FakeSession([FakeResp({}, 429, {"Retry-After": "2"}), ok])
    slept = []
    b = summarise.GeminiBackend("gk", session=sess, sleep=slept.append, min_interval=0)
    assert b.ask("p") == GOOD
    assert slept == [2.0] and len(sess.calls) == 2  # Retry-After honoured


def test_free_tier_gives_up_after_one_retry():
    sess = FakeSession([FakeResp({}, 429) for _ in range(4)])
    b = summarise.GroqBackend("qk", session=sess, sleep=lambda s: None, min_interval=0)
    with pytest.raises(RuntimeError, match="429"):
        b.ask("p")
    assert len(sess.calls) == 2


def test_long_retry_after_is_not_waited_out():
    sess = FakeSession([FakeResp({}, 429, {"Retry-After": "600"}), FakeResp({}, 200)])
    slept = []
    b = summarise.GroqBackend("qk", session=sess, sleep=slept.append, min_interval=0)
    with pytest.raises(RuntimeError, match="429"):
        b.ask("p")
    assert slept == [] and len(sess.calls) == 1  # next key instead of a 10-minute wait


def test_multiple_keys_per_provider():
    env = {"GEMINI_API_KEY": "g1\ng2", "GROQ_API_KEY": "q1, q2 ,q3", "ANTHROPIC_API_KEY": "a1"}
    names = [b.name for b in summarise.backends_from_env(env)]
    assert names == ["haiku", "gemini", "gemini", "groq", "groq", "groq"]


class Dead:
    def __init__(self, name, msg):
        self.name, self.msg, self.calls = name, msg, 0

    def ask(self, prompt):
        self.calls += 1
        raise RuntimeError(self.msg)


def test_dead_keys_are_skipped_for_rest_of_run():
    no_credit = Dead("haiku", "Error code: 400 - Your credit balance is too low")
    limited = Dead("gemini", "429 Client Error: Too Many Requests")
    good = Fixed("gemini", GOOD)
    s = summarise.Summariser([no_credit, limited, good])
    for _ in range(5):
        s.summarise(item())
    # No credit: dropped at once. Rate-limited: rests COOLDOWN_S (longer than this test), so asked once.
    assert (no_credit.calls, limited.calls, good.calls) == (1, 1, 5)
    assert s.used == {"gemini": 5}


class Flaky429(Fixed):
    """Rate-limited on the first call only."""

    def ask(self, prompt):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("429 Client Error: Too Many Requests")
        return self.reply


def test_a_rate_limited_key_rests_then_is_used_again(monkeypatch):
    flaky = Flaky429("groq", GOOD)
    s = summarise.Summariser([flaky])
    monkeypatch.setattr(summarise.time, "sleep", lambda t: None)
    monkeypatch.setattr(summarise, "COOLDOWN_S", 0.0)
    assert s.summarise(item()) == GOOD  # 429, rest, second pass
    assert 0 not in s.dead and flaky.calls == 2 and s.used == {"groq": 1}


def test_bad_summary_does_not_kill_key():
    flaky = Fixed("haiku", BAD)
    backup = Fixed("gemini", GOOD)
    s = summarise.Summariser([flaky, backup])
    s.summarise(item())
    s.summarise(item())
    assert flaky.calls == 4  # tried (with retry) on both items


# ---- the short fallback's other coverage (owner, 15 Oct 2026: fix, never hold for review) ----

from aw_workers import coverage  # noqa: E402

THIN = "Pune opened a shelter for 200 street dogs in Hadapsar."
OTHER = coverage.Report(publisher="Other Daily", url="https://other.example/pune-shelter",
                        title="Pune opens animal shelter in Hadapsar", text=SOURCE_TEXT)


def thin_item():
    return Item(NEWS, "Pune opens new animal shelter", "https://news.example/pune-shelter", date(2026, 10, 1), THIN)


def wider_backend(good=GOOD):
    b = Fixed("haiku", SHORT)
    b.ask = lambda prompt, _b=b: (setattr(_b, "calls", _b.calls + 1),
                                  good if "Another report of the same event (Other Daily)" in prompt else SHORT)[1]
    return b


def test_other_coverage_fills_a_thin_source():
    s = summarise.Summariser([wider_backend()], fetch=lambda url: "", coverage=lambda *a: [OTHER])
    assert s.summarise(thin_item()) == GOOD
    assert not s.last_short and s.last_coverage == [OTHER]  # credited by the caller, never named in the text
    assert "Other Daily" not in GOOD


def test_still_short_after_other_coverage_is_published_with_the_longer_summary():
    longer = "Pune has opened a shelter for 200 street dogs in Hadapsar. It has a veterinary clinic. Three NGOs help."
    s = summarise.Summariser([wider_backend(longer)], fetch=lambda url: "", coverage=lambda *a: [OTHER])
    assert s.summarise(thin_item()) == longer and s.last_short and s.last_coverage == [OTHER]


def test_other_coverage_cannot_bring_invented_facts():
    made_up = GOOD.replace("three local NGOs", "Blue Cross and three local NGOs")
    s = summarise.Summariser([wider_backend(made_up)], fetch=lambda url: "", coverage=lambda *a: [OTHER])
    assert s.summarise(thin_item()) == SHORT and s.last_short and not s.last_coverage


def test_coverage_failure_keeps_the_short_summary():
    def boom(*a):
        raise RuntimeError("search down")

    s = summarise.Summariser([Fixed("haiku", SHORT)], fetch=lambda url: "", coverage=boom)
    assert s.summarise(thin_item()) == SHORT and s.last_short


def test_default_coverage_search_is_offline_in_tests():
    s = summarise.Summariser([Fixed("haiku", SHORT)], fetch=lambda url: "")
    assert s.summarise(thin_item()) == SHORT and s.last_short and not s.last_coverage


def test_prompt_allows_other_reports_without_naming_them():
    assert "other reports of the same event" in summarise.prompt_for(item())


def test_rules_say_short_is_published_not_reviewed():
    rules = summary_rules.DEFAULTS
    assert "never sent to To review" in rules["fallback"] and "other coverage" in rules["fallback"]
    assert not any("goes to review" in g for g in rules["general"])


# ---- cut-off answers (owner, 20 Oct 2026: "…of Kerala's Th" was published) ----

def test_a_summary_that_ends_mid_sentence_breaks_the_rules():
    from aw_workers import summary_rules

    cut = "The council approved a new shelter for stray dogs in Pune. Officials said the work would begin after"
    found = summary_rules.problems(cut, "news")
    assert any(p.startswith("ends mid-sentence") for p in found)
    assert not summary_rules.only_short([p for p in found if p.startswith("ends mid-sentence")])


def test_a_summary_ending_on_a_quote_or_bracket_is_whole():
    from aw_workers import summary_rules

    assert summary_rules.ends_sentence("The theme was 'Revenge.'")
    assert summary_rules.ends_sentence("It was approved (by the board).")
    assert summary_rules.ends_sentence("Was it legal?”")
    assert not summary_rules.ends_sentence("in Kottoor within the Kuttichal panchayat of Kerala’s Th")


def test_whole_sentences_drops_a_cut_off_tail_never_mid_word():
    from aw_workers import summary_rules

    assert summary_rules.whole_sentences("One fact here. Two facts there. The minister ordered the transfer after") == \
        "One fact here. Two facts there."
    assert summary_rules.whole_sentences("Nothing ends here") == "Nothing ends here"
    assert summary_rules.whole_sentences("Done. ") == "Done."


def test_gemini_stopped_at_its_limit_is_a_cut_off():
    from aw_workers import summarise

    class Resp:
        status_code = 200
        headers = {}

        def raise_for_status(self):
            pass

        def json(self):
            return {"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": [{"text": "Allegations of dead dogs in Kerala’s Th"}]}}]}

    class S:
        def post(self, *a, **k):
            return Resp()

    b = summarise.GeminiBackend("k", session=S(), sleep=lambda s: None, min_interval=0)
    with pytest.raises(summarise.CutOff):
        b.ask("x")


def test_groq_stopped_at_its_limit_is_a_cut_off():
    from aw_workers import summarise

    class Resp:
        status_code = 200
        headers = {}

        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"finish_reason": "length", "message": {"content": "The minister ordered the transfer after"}}]}

    class S:
        def post(self, *a, **k):
            return Resp()

    b = summarise.GroqBackend("k", session=S(), sleep=lambda s: None, min_interval=0)
    with pytest.raises(summarise.CutOff):
        b.ask("x")
