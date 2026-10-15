"""Same event, one story (owner, 15 Oct 2026).

When a new report is the same event as a story already on the site, the first story stays the one story. The model
reads the new report's own text next to what our story already says and writes the facts the report adds, in the house
style of the summaries. The site shows them under the first story's summary, credited to the publisher
("Also reported by X, which adds: …"), and keeps them as an audit (aw_merge_additions). With nothing new, or when the
written facts fail a check, only the credit is added.

Invented facts cannot get through: the prompt gives the new report's text as the only source of facts, and every number
and every name in the written facts must appear in that text, or nothing is added. The first story's date is never
changed (the site changes post meta only).

The enrichment runs here, not on the site, because the workers hold the new report's own text (the site keeps only our
summary of it) and already rotate the keys, cool down rate-limited ones and drop dead ones.
"""

from __future__ import annotations

import json
import logging
import re

from .summarise import MAX_QUOTE_WORDS, longest_copied_run, longest_quote_words, note_failure, note_success, \
    sentence_count, usable

log = logging.getLogger("aw_workers.merge")

MAX_WORDS = 50

PROMPT = """You help keep one story per event on Animal Writes, an Indian animal-welfare site.

OUR STORY already says:
{ours}

A NEW REPORT of the same event. Its text is the only source of facts you may use:
Title: {title}
Text: {text}

Write the facts the NEW REPORT states that OUR STORY does not already say (for example a name, a number, a place,
a date, what an official, a court or the police did).
Rules:
- Use only facts written in the NEW REPORT title and text above. Never add anything from memory or from elsewhere,
  and never guess.
- Plain facts only, in your own words: who, what, where, when. No opinion, praise, advice or calls to action.
- 1 or 2 sentences, at most {max_words} words. Never quote more than {max_quote} words.
- Do not name any newspaper, channel or website, and do not mention the report itself ("the report says").
- Give a date only when the text says the event happened on that date (never the day it was published).
- Every sentence must add a specific fact (a name, a number, a place, an action). Nothing vague or general, and
  nothing OUR STORY already says in other words.
- If the new report adds nothing that our story lacks, answer with an empty value.
Answer with JSON only: {{"adds": "<the new facts, or empty>"}}"""

# Sentence-opening words that need not appear in the report (they are not facts).
_OPENERS = set("""a an the it its he she they his her their this that these those there here in on at by for from with
after before during while when according also both each officials authorities police residents locals""".split())
_MONTHS = set("january february march april may june july august september october november december".split())


def _norm_num(s: str) -> str:
    return s.replace(",", "").rstrip(".")


def _numbers(text: str) -> set[str]:
    return {_norm_num(n) for n in re.findall(r"\d[\d,.]*", text or "")}


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[\w'’-]+", (text or "").lower()))


def check(adds: str, title: str, text: str) -> list[str]:
    """Why the written facts cannot be used ([] = they can). Every number and every capitalised word (a name, a place,
    a body, a month) must be in the new report's own title or text."""
    source = f"{title} {text}"
    problems = []
    missing = sorted(_numbers(adds) - _numbers(source))
    if missing:
        problems.append("numbers not in the report: " + ", ".join(missing))
    have = _tokens(source)
    names = []
    for sentence in re.split(r"(?<=[.!?])\s+", adds.strip()):
        for i, w in enumerate(re.findall(r"[\w'’-]+", sentence)):
            if not w[:1].isupper():
                continue
            low = w.lower()
            if i == 0 and low in _OPENERS:
                continue
            if low not in have and low.rstrip("'’s") not in have:
                names.append(w)
    if names:
        problems.append("names not in the report: " + ", ".join(dict.fromkeys(names)))
    n = sentence_count(adds)
    if n > 2:
        problems.append(f"needs 1-2 sentences, got {n}")
    if len(re.findall(r"[\w'’-]+", adds)) > MAX_WORDS:
        problems.append(f"over {MAX_WORDS} words")
    if longest_quote_words(adds) > MAX_QUOTE_WORDS or longest_copied_run(adds, source) > MAX_QUOTE_WORDS:
        problems.append(f"copies more than {MAX_QUOTE_WORDS} words from the report")
    return problems


def parse(reply: str) -> str | None:
    m = re.search(r"\{.*\}", reply or "", re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict) or "adds" not in data:
        return None
    adds = re.sub(r"\s+", " ", str(data.get("adds") or "")).strip()
    return "" if adds.lower() in ("", "none", "nothing", "n/a", "empty") else adds


def prompt_for(ours: str, title: str, text: str) -> str:
    return PROMPT.format(ours=ours.strip() or "(nothing yet)", title=title.strip(), text=re.sub(r"\s+", " ", text)[:4000],
                         max_words=MAX_WORDS, max_quote=MAX_QUOTE_WORDS)


def new_facts(summariser, ours: str, title: str, text: str) -> tuple[str, str]:
    """(the facts the report adds, the model that wrote them); ('', '') = credit only."""
    text = (text or "").strip()
    backends = list(getattr(summariser, "backends", []) or [])
    if not text or not backends:
        return "", ""
    prompt = prompt_for(ours, title, text)
    for i, backend in enumerate(backends):
        if not usable(summariser, i):
            continue
        try:
            adds = parse(backend.ask(prompt))
            if adds:
                problems = check(adds, title, text)
                if problems:
                    adds = parse(backend.ask(prompt + "\n\nYour previous answer broke these rules: " + "; ".join(problems)
                                             + f"\nPrevious answer: {adds}\nAnswer again, using only the NEW REPORT text."))
                    if adds and check(adds, title, text):
                        log.info("merge: facts refused (%s); credit only", "; ".join(check(adds, title, text)))
                        return "", ""
        except Exception as exc:
            note_failure(summariser, i, exc)
            continue
        note_success(summariser, i)
        if adds is None:
            continue  # no JSON: the next provider
        return adds, f"{backend.name}:{getattr(backend, 'model', '')}"
    return "", ""


def ours_text(story: dict) -> str:
    parts = [story.get("summary") or ""] + [a.get("text", "") for a in story.get("additions") or [] if isinstance(a, dict)]
    return " ".join(p.strip() for p in parts if p and p.strip())


def payload(keep_id: int, *, url: str, publisher: str, adds: str, model: str, dup_id: int = 0) -> dict:
    body = {"keep_id": int(keep_id), "source_url": url, "publisher": publisher, "added_text": adds, "model": model if adds else ""}
    if dup_id:
        body["dup_id"] = int(dup_id)
    return body


def fold(summariser, client, keep_id, *, title: str, url: str, publisher: str, text: str, dup_id: int = 0,
         dry_run: bool = False) -> dict | None:
    """Add a same-event report to the first story: its facts (when it has new ones) and its publisher's credit.
    Never raises: a failure leaves the story as it was (the report is already held or rejected)."""
    if not keep_id or client is None or not hasattr(client, "same_story"):
        return None
    try:
        story = client.story(int(keep_id))
        done = {a.get("url") for a in story.get("additions") or [] if isinstance(a, dict)}
        adds, model = ("", "") if url in done else new_facts(summariser, ours_text(story), title, text)
        body = payload(int(keep_id), url=url, publisher=publisher, adds=adds, model=model, dup_id=dup_id)
        if dry_run:
            log.info("DRY_RUN would fold into #%s: %s", keep_id, json.dumps(body, ensure_ascii=False))
            return body
        client.same_story(body)
        log.info("merge: %s into #%s (%s)", url, keep_id, "new facts" if adds else "credit only")
        return body
    except Exception as exc:  # noqa: BLE001
        log.warning("merge: could not fold %s into #%s: %s", url, keep_id, exc)
        return None


_META_DESC = [re.compile(r'<meta[^>]+(?:property|name)=["\'](?:og:description|description)["\'][^>]+content=["\']([^"\']+)', re.I),
              re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](?:og:description|description)["\']', re.I)]


def page_text(url: str, session=None, limit: int = 4000) -> str:
    """A report's own words from its page: the description and the article's paragraphs ('' when it can't be read)."""
    import html as htmllib

    import requests

    from .enrich import TIMEOUT, UA

    try:
        resp = (session or requests).get(url, headers=UA, timeout=TIMEOUT)
        resp.raise_for_status()
        page = resp.text[:800_000]
    except Exception:  # noqa: BLE001
        return ""
    page = re.sub(r"(?is)<(script|style|noscript|nav|header|footer|aside|form)\b.*?</\1>", " ", page)
    parts = []
    for pat in _META_DESC:
        m = pat.search(page)
        if m:
            parts.append(m.group(1))
            break
    body = re.search(r"(?is)<article\b.*?</article>", page)
    for p in re.findall(r"(?is)<p\b[^>]*>(.*?)</p>", body.group(0) if body else page):
        t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", p)).strip()
        if len(t) >= 40:
            parts.append(t)
    text = " ".join(htmllib.unescape(p) for p in dict.fromkeys(parts))
    return text[:limit]
