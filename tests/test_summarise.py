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


def test_free_tier_retries_on_429_and_503_then_succeeds():
    ok = FakeResp({"candidates": [{"content": {"parts": [{"text": GOOD}]}}]})
    sess = FakeSession([FakeResp({}, 429, {"Retry-After": "2"}), FakeResp({}, 503), ok])
    slept = []
    b = summarise.GeminiBackend("gk", session=sess, sleep=slept.append, min_interval=0)
    assert b.ask("p") == GOOD
    assert slept == [2.0, 20.0] and len(sess.calls) == 3  # Retry-After honoured, then exponential backoff


def test_free_tier_gives_up_after_four_attempts():
    sess = FakeSession([FakeResp({}, 429) for _ in range(4)])
    b = summarise.GroqBackend("qk", session=sess, sleep=lambda s: None, min_interval=0)
    with pytest.raises(RuntimeError, match="429"):
        b.ask("p")
    assert len(sess.calls) == 4


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
    # No credit: dropped at once. Rate-limited: kept until RATE_LIMIT_STRIKES calls in a row ended in 429.
    assert (no_credit.calls, limited.calls, good.calls) == (1, summarise.RATE_LIMIT_STRIKES, 5)
    assert s.used == {"gemini": 5}


class Flaky429(Fixed):
    """Rate-limited on the first call only."""

    def ask(self, prompt):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("429 Client Error: Too Many Requests")
        return self.reply


def test_one_429_does_not_drop_a_key():
    flaky = Flaky429("groq", GOOD)
    s = summarise.Summariser([flaky])
    try:
        s.summarise(item())
    except summarise.SummaryError:
        pass
    assert 0 not in s.dead  # one rate limit: still in use
    s.summarise(item())
    assert flaky.calls == 2 and s.used == {"groq": 1} and s.strikes == {}


def test_bad_summary_does_not_kill_key():
    flaky = Fixed("haiku", "Too short.")
    backup = Fixed("gemini", GOOD)
    s = summarise.Summariser([flaky, backup])
    s.summarise(item())
    s.summarise(item())
    assert flaky.calls == 4  # tried (with retry) on both items
