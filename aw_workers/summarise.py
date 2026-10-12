"""Summaries via the Anthropic API, plus the checks every summary must pass."""

from __future__ import annotations

import logging
import re
import time

import requests
from datetime import datetime, timezone

from .sources import Item

log = logging.getLogger(__name__)

MAX_RETRY_WAIT = 15.0  # seconds; longer waits skip to the next key

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
- Do not say which outlets reported it or name any publication; describe only what happened.
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
            json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"maxOutputTokens": 1024}},
            timeout=30, sleep=self.sleep, min_interval=self.min_interval, state=self.state,
        )
        parts = resp.json()["candidates"][0]["content"].get("parts", [])
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
        return (resp.json()["choices"][0]["message"]["content"] or "").strip()


class OpenAICompatBackend(GroqBackend):
    """Any OpenAI-compatible chat API (OpenAI, DeepSeek, Moonshot, MiniMax)."""

    def __init__(self, name: str, url: str, api_key: str, model: str, **kw):
        self.name, self.URL = name, url
        super().__init__(api_key, model, min_interval=kw.pop("min_interval", 0.5), **kw)


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
}


def backends_from_rows(rows: list[dict], budget: float | None = None, ledger: dict | None = None) -> list:
    """One backend per saved row and model. Anthropic and Gemini use their own clients; the rest are OpenAI-compatible."""
    out = []
    for r in rows:
        key, model, provider = r["key"].strip(), (r.get("model") or "").strip(), r.get("provider") or "other"
        models = list(dict.fromkeys([m for m in [model, *EXTRA_MODELS.get(provider, [])] if m]))
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
    """Shared by summaries, topics and the relevance gate (`owner` has .dead): a dead key is dropped for the run;
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


class Summariser:
    """Tries each provider in order. A provider is used only if its summary passes every check."""

    def __init__(self, backends: list):
        if not backends:
            raise ValueError("no summary provider configured")
        self.backends = backends
        self.used: dict[str, int] = {}
        self.dead: set[int] = set()  # indexes of keys that failed hard this run
        self.cool: dict[int, float] = {}  # rate-limited keys resting until (monotonic), shared with the relevance gate

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
            source=item.publisher or item.source.name,
            title=item.title,
            date=item.date.isoformat() if item.date else "unknown",
            text=item.text[:4000] or "(no further details)",
        )
        reasons = []
        for _ in range(2):  # a second pass once a rate-limited key has rested
            for i, backend in enumerate(self.backends):
                if not usable(self, i):
                    continue
                try:
                    summary = self._try(backend, prompt, item)
                except Exception as exc:  # API error or failed checks: fall through to the next key/provider
                    note_failure(self, i, exc)
                    reasons.append(f"{backend.name}: {str(exc)[:150]}")
                    continue
                note_success(self, i)
                self.used[backend.name] = self.used.get(backend.name, 0) + 1
                return finalise(summary, item)
            if not wait_for_cooldown(self):
                break
        raise SummaryError(" | ".join(reasons))


TOPIC_PROMPT = """Choose the topics of an Indian animal-welfare news item.
Pick 1 to 3 from this list and output only their slugs, comma-separated, nothing else:
{slugs}

Title: {title}
Summary: {summary}"""


def pick_topics(summariser, item: Item, summary: str, allowed: list[str]) -> list[str]:
    """Every item gets a topic: keyword matches first; when none match, the summary model picks from the list."""
    from .sources import topics_for

    found = topics_for(item, summary)
    if found or not allowed or not hasattr(summariser, "backends"):
        return found
    prompt = TOPIC_PROMPT.format(slugs=", ".join(allowed), title=item.title, summary=summary)
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
