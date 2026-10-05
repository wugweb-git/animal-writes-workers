"""Summaries via the Anthropic API, plus the checks every summary must pass."""

from __future__ import annotations

import re

from .sources import Item

MODEL = "claude-haiku-4-5"
MAX_QUOTE_WORDS = 15
MAX_WORDS = 80
LEGAL_NOTE = "[Needs legal review]"

PROMPT = """You write short factual summaries for Animal Writes, an Indian animal-welfare site.

Summarise the item below in 2 to 3 sentences of plain English, in your own words.
Rules:
- Plain facts only: who, what, where, when. No opinion, praise, advice or calls to action.
- Do not copy phrases from the source. Never quote more than {max_quote} words.
- If the source is not in English, write the summary in English.
- If the item gives too little to summarise, state only what it says.
- Output only the summary text, nothing else.

Source: {source}
Title: {title}
Date: {date}
Details: {text}"""


class SummaryError(Exception):
    pass


def _words(text: str) -> list[str]:
    return re.findall(r"[\w'’-]+", text.lower())


def sentence_count(text: str) -> int:
    return len([s for s in re.split(r"(?<=[.!?।])\s+", text.strip()) if s])


def longest_quote_words(text: str) -> int:
    spans = re.findall(r"[\"“”]([^\"“”]{3,})[\"“”]", text)  # double quotes only; ' is too often an apostrophe
    return max((len(_words(s)) for s in spans), default=0)


def longest_copied_run(summary: str, source_text: str) -> int:
    """Length of the longest run of consecutive words shared with the source."""
    a, b = _words(summary), _words(source_text)
    if not a or not b:
        return 0
    best = 0
    prev = [0] * (len(b) + 1)
    for x in a:
        cur = [0] * (len(b) + 1)
        for j, y in enumerate(b, 1):
            if x == y:
                cur[j] = prev[j - 1] + 1
                best = max(best, cur[j])
        prev = cur
    return best


def check_summary(summary: str, item: Item) -> list[str]:
    problems = []
    n = sentence_count(summary)
    if not 2 <= n <= 3:
        problems.append(f"needs 2-3 sentences, got {n}")
    if len(_words(summary)) > MAX_WORDS:
        problems.append(f"over {MAX_WORDS} words")
    if longest_quote_words(summary) > MAX_QUOTE_WORDS:
        problems.append(f"quote longer than {MAX_QUOTE_WORDS} words")
    if longest_copied_run(summary, f"{item.title} {item.text}") > MAX_QUOTE_WORDS:
        problems.append(f"copies more than {MAX_QUOTE_WORDS} consecutive words from the source")
    return problems


def finalise(summary: str, item: Item) -> str:
    summary = re.sub(r"\s+", " ", summary).strip()
    if item.source.type == "legal":
        summary = f"{summary}\n{LEGAL_NOTE}"
    return summary


class Summariser:
    def __init__(self, api_key: str, client=None):
        if client is None:
            import anthropic

            client = anthropic.Anthropic(api_key=api_key)
        self.client = client

    def _ask(self, prompt: str) -> str:
        msg = self.client.messages.create(
            model=MODEL,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()

    def summarise(self, item: Item) -> str:
        prompt = PROMPT.format(
            max_quote=MAX_QUOTE_WORDS,
            source=item.source.name,
            title=item.title,
            date=item.date.isoformat() if item.date else "unknown",
            text=item.text[:4000] or "(no further details)",
        )
        summary = self._ask(prompt)
        problems = check_summary(summary, item)
        if problems:
            summary = self._ask(
                prompt + "\n\nYour previous attempt broke these rules: " + "; ".join(problems)
                + f"\nPrevious attempt: {summary}\nWrite a corrected summary."
            )
            problems = check_summary(summary, item)
        if problems:
            raise SummaryError("; ".join(problems))
        return finalise(summary, item)


class DryRunSummariser:
    """Used in DRY_RUN when no API key is available: no network, clearly marked output."""

    def summarise(self, item: Item) -> str:
        return finalise("[DRY RUN: summary not generated.] [DRY RUN: no API key set.]", item)
