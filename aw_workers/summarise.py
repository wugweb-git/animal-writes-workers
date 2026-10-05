"""Summaries via the Anthropic API, plus the checks every summary must pass."""

from __future__ import annotations

import re
from datetime import datetime, timezone

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


# Claude Haiku 4.5 list prices, USD per token.
HAIKU_INPUT_USD = 1.00 / 1_000_000
HAIKU_OUTPUT_USD = 5.00 / 1_000_000


class BudgetExceeded(Exception):
    pass


class ClaudeBackend:
    """Claude Haiku 4.5, capped by an estimated monthly spend (ledger persisted between runs)."""

    name = "haiku"

    def __init__(self, api_key: str, client=None, budget_usd: float | None = None,
                 ledger: dict | None = None, month: str | None = None):
        if client is None:
            import anthropic

            client = anthropic.Anthropic(api_key=api_key)
        self.client = client
        self.budget_usd = budget_usd
        self.ledger = ledger if ledger is not None else {}
        self.month = month or datetime.now(timezone.utc).strftime("%Y-%m")

    @property
    def spent_usd(self) -> float:
        return float(self.ledger.get(self.month, 0.0))

    def ask(self, prompt: str) -> str:
        if self.budget_usd is not None and self.spent_usd >= self.budget_usd:
            raise BudgetExceeded(f"monthly Haiku budget ${self.budget_usd:.2f} reached")
        msg = self.client.messages.create(
            model=MODEL,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        usage = getattr(msg, "usage", None)
        if usage is not None:
            cost = usage.input_tokens * HAIKU_INPUT_USD + usage.output_tokens * HAIKU_OUTPUT_USD
            self.ledger[self.month] = round(self.spent_usd + cost, 6)
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()


class GeminiBackend:
    """Google Gemini REST API (free tier)."""

    name = "gemini"
    URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    def __init__(self, api_key: str, model: str = "gemini-flash-latest", session=None):
        import requests

        self.api_key, self.model = api_key, model
        self.session = session or requests.Session()

    def ask(self, prompt: str) -> str:
        resp = self.session.post(
            self.URL.format(model=self.model),
            headers={"x-goog-api-key": self.api_key},
            json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"maxOutputTokens": 1024}},
            timeout=60,
        )
        resp.raise_for_status()
        parts = resp.json()["candidates"][0]["content"].get("parts", [])
        return "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()


class GroqBackend:
    """Groq OpenAI-compatible chat API (free tier)."""

    name = "groq"
    URL = "https://api.groq.com/openai/v1/chat/completions"

    def __init__(self, api_key: str, model: str = "llama-3.3-70b-versatile", session=None):
        import requests

        self.api_key, self.model = api_key, model
        self.session = session or requests.Session()

    def ask(self, prompt: str) -> str:
        resp = self.session.post(
            self.URL,
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "max_tokens": 300, "messages": [{"role": "user", "content": prompt}]},
            timeout=60,
        )
        resp.raise_for_status()
        return (resp.json()["choices"][0]["message"]["content"] or "").strip()


def backends_from_env(env, ledger: dict | None = None) -> list:
    """Providers in LLM_PROVIDERS order (default haiku,gemini,groq); ones without a key are skipped.
    Haiku stops for the month once its estimated spend reaches HAIKU_MONTHLY_BUDGET_USD (default 4)."""
    budget = float(env.get("HAIKU_MONTHLY_BUDGET_USD") or 4)
    makers = {
        "haiku": lambda: env.get("ANTHROPIC_API_KEY")
        and ClaudeBackend(env["ANTHROPIC_API_KEY"], budget_usd=budget, ledger=ledger),
        "gemini": lambda: env.get("GEMINI_API_KEY")
        and GeminiBackend(env["GEMINI_API_KEY"], env.get("GEMINI_MODEL") or "gemini-flash-latest"),
        "groq": lambda: env.get("GROQ_API_KEY")
        and GroqBackend(env["GROQ_API_KEY"], env.get("GROQ_MODEL") or "llama-3.3-70b-versatile"),
    }
    order = [p.strip().lower() for p in (env.get("LLM_PROVIDERS") or "haiku,gemini,groq").split(",") if p.strip()]
    unknown = [p for p in order if p not in makers]
    if unknown:
        raise ValueError(f"unknown LLM_PROVIDERS entries: {', '.join(unknown)}")
    return [b for b in (makers[p]() for p in order) if b]


class Summariser:
    """Tries each provider in order. A provider is used only if its summary passes every check."""

    def __init__(self, backends: list):
        if not backends:
            raise ValueError("no summary provider configured")
        self.backends = backends
        self.used: dict[str, int] = {}

    def _try(self, backend, prompt: str, item: Item) -> str:
        summary = backend.ask(prompt)
        problems = check_summary(summary, item)
        if problems:
            summary = backend.ask(
                prompt + "\n\nYour previous attempt broke these rules: " + "; ".join(problems)
                + f"\nPrevious attempt: {summary}\nWrite a corrected summary."
            )
            problems = check_summary(summary, item)
        if problems:
            raise SummaryError("; ".join(problems))
        return summary

    def summarise(self, item: Item) -> str:
        prompt = PROMPT.format(
            max_quote=MAX_QUOTE_WORDS,
            source=item.source.name,
            title=item.title,
            date=item.date.isoformat() if item.date else "unknown",
            text=item.text[:4000] or "(no further details)",
        )
        reasons = []
        for backend in self.backends:
            try:
                summary = self._try(backend, prompt, item)
            except Exception as exc:  # API error or failed checks: fall through to the next provider
                reasons.append(f"{backend.name}: {str(exc)[:150]}")
                continue
            self.used[backend.name] = self.used.get(backend.name, 0) + 1
            return finalise(summary, item)
        raise SummaryError(" | ".join(reasons))


class DryRunSummariser:
    """Used in DRY_RUN when no provider key is available: no network, clearly marked output."""

    used: dict = {}

    def summarise(self, item: Item) -> str:
        return finalise("[DRY RUN: summary not generated.] [DRY RUN: no API key set.]", item)
