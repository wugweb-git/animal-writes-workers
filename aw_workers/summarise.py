"""Summaries via the Anthropic API, plus the checks every summary must pass."""

from __future__ import annotations

import dataclasses
import logging
import re
import time

import requests
from datetime import datetime, timezone

from . import ai_usage, event_dates, summary_rules
from .sources import Item

log = logging.getLogger(__name__)

MAX_RETRY_WAIT = 15.0  # seconds; longer waits skip to the next key

MODEL = "claude-haiku-4-5"
MAX_QUOTE_WORDS = summary_rules.DEFAULTS["max_quote_words"]  # kept for older callers; the rules decide
LEGAL_NOTE = "[Needs legal review]"

# The rules themselves (length per section, structure, attribution, quotes, wording) come from Settings → Sourcing →
# Summary rules on the site (summary_rules.py); this is only the frame around them.
PROMPT = """You write factual summaries for Animal Writes, an Indian animal-welfare site. This one is for its {section_label}.

Summarise the item below in English. Follow every rule:
{rules}
- The details may include other reports of the same event: use their facts too, never the names of their publishers.
- Output only the summary text, nothing else.

Source: {source}
Title: {title}
Published: {date} (the day the report came out, not the date of the event)
Details: {text}"""


class CutOff(Exception):
    """The provider stopped at its output limit: the answer ends mid-sentence (20 Oct 2026: Gemini's thinking used
    up maxOutputTokens and "…of Kerala's Th" was published). The next key or provider is tried."""


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


def section_of(item: Item) -> str:
    return summary_rules.section_for(item.source.type)


def check_summary(summary: str, item: Item, rules: dict | None = None) -> list[str]:
    """Why the summary breaks the summary rules ([] = it follows them)."""
    return summary_rules.problems(summary, section_of(item), f"{item.title}. {item.text}", rules,
                                  publisher=item.publisher or "")


def date_problems(summary: str, item: Item) -> list[str]:
    """The publication date given as the event date (owner, 15 Oct 2026): a date in the summary that equals the item's
    date, when the title and text do not say the event happened then."""
    wrong = event_dates.pub_date_mentions(summary, [item.date], f"{item.title}. {item.text}") if item.date else []
    return [event_dates.problem(wrong)] if wrong else []


def number_problems(summary: str, item: Item) -> list[str]:
    """Numbers in the summary that the source does not give (owner, 20 Oct 2026); the item's own date is allowed."""
    allowed = {str(item.date.year), str(item.date.day)} if item.date else set()
    extra = summary_rules.unsourced_numbers(summary, f"{item.title}. {item.text}", allowed)
    return [f"numbers not in the source: {', '.join(extra)} (use only numbers the source gives, exactly as it gives them)"] if extra else []


def drop_pub_dates(summary: str, item: Item) -> str:
    """The summary with such a date phrase taken out (a sentence that only gave the date goes); unchanged when there is
    none or it can't be taken out cleanly (then the checks refuse it)."""
    if not item.date:
        return summary
    wrong = event_dates.pub_date_mentions(summary, [item.date], f"{item.title}. {item.text}")
    if not wrong:
        return summary
    out = event_dates.remove_dates(summary, wrong)
    return out if out and event_dates.clean_removal(summary, out) else summary


def finalise(summary: str, item: Item) -> str:
    summary = summary_rules.whole_sentences(re.sub(r"\s+", " ", summary).strip())  # never a cut-off tail
    if getattr(item, "draft", False) and "draft" not in summary.lower():  # a draft is never written up as rules in force
        summary = f"Draft, open for objections: {summary}"
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
                 ledger: dict | None = None, month: str | None = None, model: str = MODEL):
        self.model, self.api_key = model, api_key
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
            model=self.model,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        if getattr(msg, "stop_reason", "") == "max_tokens":
            raise CutOff(f"{self.name}: stopped at max_tokens")
        usage = getattr(msg, "usage", None)
        if usage is not None:
            cost = usage.input_tokens * HAIKU_INPUT_USD + usage.output_tokens * HAIKU_OUTPUT_USD
            self.ledger[self.month] = round(self.spent_usd + cost, 6)
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()


def _post_with_retry(session, url, *, headers, json, timeout, sleep, min_interval, state):
    """Free tiers are rate-limited: keep a minimum gap between calls and retry a 429/5xx once, after a short wait.
    A key that asks to wait longer than MAX_RETRY_WAIT is given up for this call at once (12 Oct 2026: waiting out
    Groq's Retry-After held a whole run for 20 minutes); the next key is tried instead."""
    import time

    wait = min_interval - (time.monotonic() - state.get("last", 0.0))
    if wait > 0:
        sleep(wait)
    for attempt in range(2):
        state["last"] = time.monotonic()
        resp = session.post(url, headers=headers, json=json, timeout=timeout)
        if (resp.status_code == 429 or resp.status_code >= 500) and attempt == 0:
            retry_after = str((getattr(resp, "headers", None) or {}).get("Retry-After", "")).strip()
            delay = float(retry_after) if retry_after.replace(".", "", 1).isdigit() else 5.0
            if delay > MAX_RETRY_WAIT:
                break
            sleep(delay)
            continue
        break
    resp.raise_for_status()
    return resp


class GeminiBackend:
    """Google Gemini REST API (free tier)."""

    name = "gemini"
    URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    def __init__(self, api_key: str, model: str = "gemini-flash-latest", session=None, sleep=None,
                 min_interval: float = 7.0):
        import time

        import requests

        self.api_key, self.model = api_key, model
        self.session = session or requests.Session()
        self.sleep, self.min_interval, self.state = sleep or time.sleep, min_interval, {}

    def ask(self, prompt: str) -> str:
        resp = _post_with_retry(
            self.session,
            self.URL.format(model=self.model),
            headers={"x-goog-api-key": self.api_key},
            # 4096: flash models think first, and the thinking counts against this limit (1024 cut answers off)
            json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"maxOutputTokens": 4096}},
            timeout=30, sleep=self.sleep, min_interval=self.min_interval, state=self.state,
        )
        cand = resp.json()["candidates"][0]
        if cand.get("finishReason") == "MAX_TOKENS":
            raise CutOff(f"{self.name}: stopped at maxOutputTokens")
        parts = cand.get("content", {}).get("parts", [])
        return "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()


class GroqBackend:
    """Groq OpenAI-compatible chat API (free tier)."""

    name = "groq"
    URL = "https://api.groq.com/openai/v1/chat/completions"

    def __init__(self, api_key: str, model: str = "openai/gpt-oss-120b", session=None, sleep=None,
                 min_interval: float = 3.0):
        import time

        import requests

        self.api_key, self.model = api_key, model
        self.session = session or requests.Session()
        self.sleep, self.min_interval, self.state = sleep or time.sleep, min_interval, {}

    def ask(self, prompt: str) -> str:
        resp = _post_with_retry(
            self.session,
            self.URL,
            headers={"Authorization": f"Bearer {self.api_key}"},
            # 800: gpt-oss models reason before answering, and 300 tokens could end before the answer (12 Oct 2026)
            json={"model": self.model, "max_tokens": 800, "messages": [{"role": "user", "content": prompt}]},
            timeout=30, sleep=self.sleep, min_interval=self.min_interval, state=self.state,
        )
        choice = resp.json()["choices"][0]
        if choice.get("finish_reason") == "length":
            raise CutOff(f"{self.name}: stopped at max_tokens")
        return (choice["message"]["content"] or "").strip()


class OpenAICompatBackend(GroqBackend):
    """Any OpenAI-compatible chat API (OpenAI, DeepSeek, Moonshot, MiniMax)."""

    def __init__(self, name: str, url: str, api_key: str, model: str, **kw):
        self.name, self.URL = name, url
        super().__init__(api_key, model, min_interval=kw.pop("min_interval", 0.5), **kw)


# Every call is counted per key for Settings → AI models (ai_usage.py; owner, 20 Oct 2026).
for _cls in (ClaudeBackend, GeminiBackend, GroqBackend, OpenAICompatBackend):
    if "ask" in _cls.__dict__:
        _cls.ask = ai_usage.counted(_cls.__dict__["ask"])

# name: (URL, key env var, model env var, default model)
OPENAI_COMPAT = {
    "deepseek": ("https://api.deepseek.com/chat/completions", "DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "deepseek-chat"),
    "openai": ("https://api.openai.com/v1/chat/completions", "OPENAI_API_KEY", "OPENAI_MODEL", "gpt-4o-mini"),
    "moonshot": ("https://api.moonshot.ai/v1/chat/completions", "MOONSHOT_API_KEY", "MOONSHOT_MODEL", "moonshot-v1-8k"),
    "minimax": ("https://api.minimax.io/v1/chat/completions", "MINIMAX_API_KEY", "MINIMAX_MODEL", "MiniMax-M2"),
    "xai": ("https://api.x.ai/v1/chat/completions", "XAI_API_KEY", "XAI_MODEL", "grok-3-mini"),
    "openrouter": ("https://openrouter.ai/api/v1/chat/completions", "OPENROUTER_API_KEY", "OPENROUTER_MODEL", "openai/gpt-4o-mini"),
}


def _keys(*values: str | None) -> list[str]:
    """A secret may hold several keys, one per line (or comma-separated); FOO and FOO_2 are read together."""
    out = []
    for value in values:
        out += [k.strip() for k in re.split(r"[\n,]", value or "") if k.strip() and k.strip() not in out]
    return out


def site_rows(env, session=None) -> list[dict]:
    """Rows the owner saved on the site (Settings → AI models): provider, name, model, url, key, in the order
    to try them. [] when the site can't be reached (the GitHub secrets still work)."""
    base, user, pw = env.get("WP_BASE_URL"), env.get("WP_INGEST_USER"), env.get("WP_INGEST_APP_PASSWORD")
    if not (base and user and pw) or env.get("AW_SITE_KEYS") == "0":
        return []
    try:
        resp = (session or requests).get(base.rstrip("/") + "/wp-json/aw/v1/ai-keys", auth=(user, pw), timeout=30)
        resp.raise_for_status()
        rows = resp.json().get("rows") or []
    except Exception as exc:  # noqa: BLE001
        log.warning("site AI models not read: %s", type(exc).__name__)
        return []
    return [r for r in rows if isinstance(r, dict) and str(r.get("key") or "").strip()]


# Free tiers limit each model separately, so a free key is used once per model (12 Oct 2026: one model per key
# left 199 items unchecked while the same keys' other models were idle). Tried in this order after the saved model.
EXTRA_MODELS = {
    "gemini": ["gemini-flash-lite-latest", "gemini-3-flash-preview", "gemini-flash-latest"],
    "groq": ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "llama-3.3-70b-versatile", "llama-3.1-8b-instant"],
    "openrouter": ["minimax/minimax-m3.1-flash:free", "nvidia/nemotron-3-ultra-550b-a55b:free", "google/gemma-4-31b-it:free"],
    "requesty": ["google/gemma-4-31b-it", "nvidia/nemotron-3-ultra-550b-a55b"],
}
# Free models a key is tried on BEFORE its saved model (owner, 22 Oct 2026: use the daily free quotas, pay only after).
FREE_FIRST = {"openrouter", "requesty"}


def backends_from_rows(rows: list[dict], budget: float | None = None, ledger: dict | None = None) -> list:
    """One backend per saved row and model. Anthropic and Gemini use their own clients; the rest are OpenAI-compatible."""
    out = []
    for r in rows:
        key, model, provider = r["key"].strip(), (r.get("model") or "").strip(), r.get("provider") or "other"
        extra = EXTRA_MODELS.get(provider, [])
        models = list(dict.fromkeys([m for m in ([*extra, model] if provider in FREE_FIRST else [model, *extra]) if m]))
        if provider == "anthropic":
            out.append(ClaudeBackend(key, budget_usd=budget, ledger=ledger, model=model or MODEL))
        elif provider == "gemini":
            out += [GeminiBackend(key, m) for m in models or ["gemini-flash-latest"]]
        elif r.get("url") and models:
            out += [OpenAICompatBackend(r.get("name") or provider, r["url"], key, m) for m in models]
        else:
            log.warning("AI model row skipped (%s): needs a model and an API address", r.get("name") or provider)
    return out


def backends_from_env(env, ledger: dict | None = None) -> list:
    """Providers in LLM_PROVIDERS order (default haiku,gemini,groq), one backend per key.
    Providers without a key are skipped. Haiku stops for the month once its estimated spend
    reaches HAIKU_MONTHLY_BUDGET_USD (default 4), shared across all Haiku keys."""
    budget = float(env.get("HAIKU_MONTHLY_BUDGET_USD") or 4)
    ledger = ledger if ledger is not None else {}
    makers = {
        "haiku": lambda: [ClaudeBackend(k, budget_usd=budget, ledger=ledger) for k in _keys(env.get("ANTHROPIC_API_KEY"), env.get("ANTHROPIC_API_KEY_2"))],
        "gemini": lambda: [GeminiBackend(k, env.get("GEMINI_MODEL") or "gemini-flash-latest")
                           for k in _keys(env.get("GEMINI_API_KEY"), env.get("GEMINI_API_KEY_2"))],
        "groq": lambda: [GroqBackend(k, env.get("GROQ_MODEL") or "openai/gpt-oss-120b")
                         for k in _keys(env.get("GROQ_API_KEY"), env.get("GROQ_API_KEY_2"))],
    }
    for name, (url, key_var, model_var, model) in OPENAI_COMPAT.items():
        makers[name] = (lambda n=name, u=url, kv=key_var, mv=model_var, m=model:
                        [OpenAICompatBackend(n, u, k, env.get(mv) or m) for k in _keys(env.get(kv), env.get(kv + "_2"))])
    order = [p.strip().lower() for p in (env.get("LLM_PROVIDERS") or "haiku,gemini,groq").split(",") if p.strip()]
    unknown = [p for p in order if p not in makers]
    if unknown:
        raise ValueError(f"unknown LLM_PROVIDERS entries: {', '.join(unknown)}")
    ledger = ledger if ledger is not None else {}
    first = backends_from_rows(site_rows(env), budget, ledger)  # the owner's saved rows, top down
    used = {b.api_key for b in first}
    return first + [b for p in order for b in makers[p]() if b.api_key not in used]  # a key saved in both: once


# Errors that mean "this key is unusable for the rest of the run" (not a bad summary).
_DEAD_KEY_MARKERS = ("401", "402", "403", "credit balance", "budget", "quota", "insufficient", "no credits", "suspended")
# A 429 (rate limit) is not a dead key: _post_with_retry already waited and retried; the key is dropped for the run only
# after this many calls in a row still ended in 429 (12 Oct 2026: one 429 used to drop Groq, the only working key).


def _is_dead_key_error(exc: Exception) -> bool:
    if isinstance(exc, SummaryError):
        return False
    text = f"{type(exc).__name__} {exc}".lower()
    return isinstance(exc, BudgetExceeded) or any(m in text for m in _DEAD_KEY_MARKERS)


def _is_rate_limit(exc: Exception) -> bool:
    return "429" in f"{exc}" or "too many requests" in f"{exc}".lower()


COOLDOWN_S = 60.0  # free tiers limit per minute: a rate-limited key rests this long, then is used again


def note_failure(owner, i: int, exc: Exception) -> None:
    """Shared by summaries, categories and the relevance gate (`owner` has .dead): a dead key is dropped for the run;
    a key still rate-limited after its retry rests COOLDOWN_S and is then used again (12 Oct 2026: dropping it for
    the whole run left 199 items unchecked while Groq's per-minute limit had long reset)."""
    if _is_dead_key_error(exc):
        owner.dead.add(i)
    elif _is_rate_limit(exc):
        owner.__dict__.setdefault("cool", {})[i] = time.monotonic() + COOLDOWN_S


def note_success(owner, i: int) -> None:
    owner.__dict__.setdefault("cool", {}).pop(i, None)


def usable(owner, i: int) -> bool:
    return i not in owner.dead and owner.__dict__.get("cool", {}).get(i, 0) <= time.monotonic()


def wait_for_cooldown(owner, sleep=None) -> bool:
    """Every live key is resting: wait for the first to come back (True), or False when none is resting."""
    resting = [t for i, t in owner.__dict__.get("cool", {}).items() if i not in owner.dead]
    if not resting:
        return False
    (sleep or time.sleep)(max(0.0, min(resting) - time.monotonic()))
    return True


class ShortSummary(SummaryError):
    """A summary that follows every rule but is below the section's minimum: the source gave too little."""

    def __init__(self, summary: str, why: str):
        super().__init__(why)
        self.summary = summary


def _readable_url(url: str) -> bool:
    return bool(re.match(r"https?://[^/]+\.[^/]+/", url or "")) and not url.lower().split("?")[0].endswith(".pdf")


def thin_source(item: Item, rules: dict | None = None) -> bool:
    """The feed gave fewer words than twice the section's minimum: a longer summary would have to be padded."""
    rules = rules or summary_rules.current()
    sec = rules["sections"].get(section_of(item)) or rules["sections"]["news"]
    return len(summary_rules.words(f"{item.title} {item.text}")) < 2 * int(sec["min_words"])


def prompt_for(item: Item, rules: dict | None = None) -> str:
    rules = rules or summary_rules.current()
    section = section_of(item)
    return PROMPT.format(
        section_label=rules["sections"].get(section, {}).get("label", section),
        rules=summary_rules.rules_block(section, rules, f"{item.title}. {item.text}"),
        source=item.publisher or item.source.name,
        title=item.title,
        date=item.date.isoformat() if item.date else "unknown",
        text=item.text[:6000] or "(no further details)",
    )


class Summariser:
    """Tries each provider in order. A provider is used only if its summary passes every check.

    The short fallback (owner, 15 Oct 2026: "fix the summaries instead of putting them under review"): a summary below
    the section's minimum is never padded and never held for review.
    1. The source page is read again when the feed gave too little, and the summary written from it.
    2. Still short (or the page is thin or refuses us): other coverage of the same event is found (coverage.py: a
       Google News search with the key names and the dates around the report) and the summary is written from the
       facts of all of them. Every name and number must be in one of those texts. Their publishers are kept in
       `last_coverage` so the caller credits them as logos in the source row (aw/v1/same-story), never in the text.
    3. Still short: the best summary that follows every other rule is published (`last_short` is set so the site can
       mark it for a later pass; it is not held). Accurate and complete beats long."""

    def __init__(self, backends: list, fetch=None, coverage=None):
        if not backends:
            raise ValueError("no summary provider configured")
        self.backends = backends
        self.used: dict[str, int] = {}
        self.dead: set[int] = set()  # indexes of keys that failed hard this run
        self.cool: dict[int, float] = {}  # rate-limited keys resting until (monotonic), shared with the relevance gate
        self.fetch = fetch  # url -> the page's own text ('' when unreadable); default: merge.page_text
        self.coverage = coverage  # (title, url, date, publisher) -> [coverage.Report]; default: coverage.find
        self.last_short = False
        self.last_coverage: list = []

    def _try(self, backend, prompt: str, item: Item) -> str:
        summary = backend.ask(prompt)
        problems = check_summary(summary, item) + date_problems(summary, item)
        if summary_rules.only_short(problems) and thin_source(item):
            raise ShortSummary(summary, "; ".join(problems))  # asking again would only pad: read the page instead
        problems += number_problems(summary, item)
        if problems:
            summary = backend.ask(
                prompt + "\n\nYour previous attempt broke these rules: " + "; ".join(problems)
                + f"\nPrevious attempt: {summary}\nWrite a corrected summary."
            )
            summary = drop_pub_dates(summary, item)  # still the publication date: the date phrase goes
            problems = check_summary(summary, item) + date_problems(summary, item)
        if summary_rules.only_short(problems):
            raise ShortSummary(summary, "; ".join(problems))
        if problems:
            raise SummaryError("; ".join(problems))
        # A number the source does not give, still there after the second ask: the item waits for a person.
        self.last_numbers = number_problems(summary, item)
        return summary

    def _summarise(self, item: Item) -> str:
        prompt = prompt_for(item)
        reasons = []
        for _ in range(2):  # a second pass once a rate-limited key has rested
            for i, backend in enumerate(self.backends):
                if not usable(self, i):
                    continue
                try:
                    summary = self._try(backend, prompt, item)
                except ShortSummary:
                    note_success(self, i)
                    self.used[backend.name] = self.used.get(backend.name, 0) + 1
                    raise  # the source gave too little: another model would not find more in it
                except Exception as exc:  # API error or failed checks: fall through to the next key/provider
                    note_failure(self, i, exc)
                    reasons.append(f"{backend.name}: {str(exc)[:150]}")
                    continue
                note_success(self, i)
                self.used[backend.name] = self.used.get(backend.name, 0) + 1
                return summary
            if not wait_for_cooldown(self):
                break
        raise SummaryError(" | ".join(reasons))

    def summarise(self, item: Item) -> str:
        self.last_short, self.last_coverage, self.last_numbers = False, [], []
        try:
            return finalise(self._summarise(item), item)
        except ShortSummary as short:
            kept = short
        fetch, deep = self.fetch, None
        if fetch is None:
            from .firecrawl import page_text as deep  # the page hides its text from a plain read (22 Oct 2026)
            from .merge import page_text as fetch
        more = fetch(item.url) if _readable_url(item.url) else ""
        if deep and _readable_url(item.url) and len((more or "").strip()) <= len(item.text or "") + 200:
            more = max(more or "", deep(item.url), key=len)
        base = item
        if len((more or "").strip()) > len(item.text or "") + 200:
            base = dataclasses.replace(item, text=more)
            try:
                return finalise(self._summarise(base), item)
            except ShortSummary as again:
                kept = again
        reports = self._other_coverage(item)
        if reports:
            from . import coverage

            wider = dataclasses.replace(base, text=coverage.combined_text(base.text, reports))
            try:
                summary = self._summarise(wider)
            except ShortSummary as again:
                summary = again.summary if len(summary_rules.words(again.summary)) > len(summary_rules.words(kept.summary)) else ""
                done = False
            except SummaryError as exc:  # every provider failed on the wider text: the short summary stands
                log.info("summary from other coverage failed: %s (%s)", item.url, str(exc)[:200])
                summary, done = "", False
            else:
                done = True
            if summary and not invented(summary, f"{item.title}. {wider.text}"):
                self.last_coverage = reports
                if done:
                    return finalise(summary, item)
                kept = ShortSummary(summary, "short after other coverage")
        self.last_short = True
        log.info("summary below the minimum after the source and other coverage; published as it is: %s (%s)",
                 item.url, kept)
        return finalise(kept.summary, item)

    def _other_coverage(self, item: Item) -> list:
        find = self.coverage
        if find is None:
            from .coverage import find
        try:
            return list(find(item.title, item.url, item.date, item.publisher or item.source.name) or [])
        except Exception as exc:  # noqa: BLE001
            log.info("other coverage not found for %s: %s", item.url, type(exc).__name__)
            return []


def invented(summary: str, source_text: str) -> list[str]:
    """Numbers and names in a summary written from several reports that none of them contains (merge.py's check)."""
    from .merge import _unsourced

    names = " ".join(summary_rules.entities_in(source_text))
    return _unsourced(summary, f"{source_text} {names}")


CATEGORY_PROMPT = """Choose the categories of an Indian animal-welfare news item.
Pick 1 to 3 from this list and output only their slugs, comma-separated, nothing else:
{slugs}

Title: {title}
Summary: {summary}"""


def pick_categories(summariser, item: Item, summary: str, allowed: list[str]) -> list[str]:
    """Every item gets a category: keyword matches first; when none match, the summary model picks from the list."""
    from .sources import categories_for

    found = [c for c in categories_for(item, summary) if not allowed or c in allowed]
    if found or not allowed or not hasattr(summariser, "backends"):
        return found
    prompt = CATEGORY_PROMPT.format(slugs=", ".join(allowed), title=item.title, summary=summary)
    for i, backend in enumerate(summariser.backends):
        if not usable(summariser, i):
            continue
        try:
            reply = backend.ask(prompt)
        except Exception as exc:
            note_failure(summariser, i, exc)
            continue
        note_success(summariser, i)
        picked = [s for s in re.findall(r"[a-z][a-z-]+", reply.lower()) if s in allowed]
        if picked:
            return list(dict.fromkeys(picked))[:3]
    return []


class DryRunSummariser:
    """Used in DRY_RUN when no provider key is available: no network, clearly marked output."""

    used: dict = {}

    def summarise(self, item: Item) -> str:
        return finalise("[DRY RUN: summary not generated.] [DRY RUN: no API key set.]", item)
