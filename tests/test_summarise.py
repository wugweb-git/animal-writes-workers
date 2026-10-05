from datetime import date
from types import SimpleNamespace

import pytest

from aw_workers import summarise
from aw_workers.sources import Item, Source

NEWS = Source("n", "News", "rss", "news", "https://x")
LEGAL = Source("l", "AWBI", "awbi", "legal", "https://x")
SOURCE_TEXT = (
    "The district administration in Pune opened a new animal shelter on Monday that will house up to "
    "two hundred street dogs and provide free vaccination and sterilisation for community animals."
)


def item(src=NEWS):
    return Item(src, "Pune opens new animal shelter", "https://x/1", date(2026, 10, 1), SOURCE_TEXT)


GOOD = "Pune's district administration has opened an animal shelter. It can hold about 200 dogs and offers free vaccination."


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
        ("One sentence only.", "2-3 sentences"),
        ("One. Two. Three. Four.", "2-3 sentences"),
        (" ".join(["Word"] * 85) + ". Second sentence.", "over 80 words"),
        ('Officials said "this is a very long quoted statement that goes on well past the fifteen word limit we set for quotes". It opened.', "quote longer"),
    ],
)
def test_bad_summaries_fail(text, problem):
    assert any(problem in p for p in summarise.check_summary(text, item()))


def test_copied_run_detected():
    copied = SOURCE_TEXT.split(" on Monday")[1] + ". It is in Pune."
    assert summarise.longest_copied_run(copied, SOURCE_TEXT) > 15
    assert any("consecutive words" in p for p in summarise.check_summary(copied, item()))


def test_quote_of_fifteen_words_allowed():
    q = " ".join(["word"] * 15)
    assert summarise.longest_quote_words(f'He said "{q}". Then left.') == 15


def test_retry_then_success():
    client = FakeClient(["Too short.", GOOD])
    out = summarise.Summariser([summarise.ClaudeBackend("k", client=client)]).summarise(item())
    assert out == GOOD and client.calls == 2


def test_gives_up_after_retry():
    client = FakeClient(["Too short.", "Still short."])
    with pytest.raises(summarise.SummaryError):
        summarise.Summariser([summarise.ClaudeBackend("k", client=client)]).summarise(item())


def test_legal_note_added():
    out = summarise.Summariser([summarise.ClaudeBackend("k", client=FakeClient([GOOD]))]).summarise(item(LEGAL))
    assert out.endswith("\n[Needs legal review]")
    news = summarise.Summariser([summarise.ClaudeBackend("k", client=FakeClient([GOOD]))]).summarise(item())
    assert "legal review" not in news


class FakeResp:
    def __init__(self, data, code=200):
        self._data, self.status_code = data, code

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
    b = summarise.GeminiBackend("gk", session=sess)
    assert b.ask("p") == GOOD
    url, headers, body = sess.calls[0]
    assert url.endswith("/models/gemini-flash-latest:generateContent")
    assert headers == {"x-goog-api-key": "gk"} and body["contents"][0]["parts"][0]["text"] == "p"


def test_groq_request_and_parse():
    sess = FakeSession([FakeResp({"choices": [{"message": {"content": GOOD}}]})])
    b = summarise.GroqBackend("qk", session=sess)
    assert b.ask("p") == GOOD
    url, headers, body = sess.calls[0]
    assert url == "https://api.groq.com/openai/v1/chat/completions"
    assert headers["Authorization"] == "Bearer qk" and body["model"] == "llama-3.3-70b-versatile"


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
    bad_quality = Fixed("gemini", "Too short.")
    groq = Fixed("groq", GOOD)
    s = summarise.Summariser([Failing(), bad_quality, groq])
    assert s.summarise(item()) == GOOD
    assert bad_quality.calls == 2 and s.used == {"groq": 1}


def test_all_providers_fail():
    with pytest.raises(summarise.SummaryError, match="haiku: overloaded"):
        summarise.Summariser([Failing(), Fixed("groq", "Short.")]).summarise(item())


def test_backends_from_env_order_and_skips_missing_keys():
    env = {"GEMINI_API_KEY": "g", "GROQ_API_KEY": "q"}
    assert [b.name for b in summarise.backends_from_env(env)] == ["gemini", "groq"]
    env["ANTHROPIC_API_KEY"] = "a"
    assert [b.name for b in summarise.backends_from_env(env)] == ["haiku", "gemini", "groq"]
    env["LLM_PROVIDERS"] = "groq, haiku"
    assert [b.name for b in summarise.backends_from_env(env)] == ["groq", "haiku"]
    with pytest.raises(ValueError):
        summarise.backends_from_env({"LLM_PROVIDERS": "deepseek"})


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
