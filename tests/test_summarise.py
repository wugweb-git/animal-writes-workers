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
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.replies.pop(0))])


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
    out = summarise.Summariser("k", client=client).summarise(item())
    assert out == GOOD and client.calls == 2


def test_gives_up_after_retry():
    client = FakeClient(["Too short.", "Still short."])
    with pytest.raises(summarise.SummaryError):
        summarise.Summariser("k", client=client).summarise(item())


def test_legal_note_added():
    out = summarise.Summariser("k", client=FakeClient([GOOD])).summarise(item(LEGAL))
    assert out.endswith("\n[Needs legal review]")
    news = summarise.Summariser("k", client=FakeClient([GOOD])).summarise(item())
    assert "legal review" not in news
