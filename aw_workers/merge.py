"""Same event, one story (owner, 15 Oct 2026).

When a new report is the same event as a story already on the site, the first story stays the one story. When the
report has facts our story lacks, the model rewrites the first story's summary with them woven in: one better summary in
the house style, reading like edited journalism (no templated "also reported" block, no stock phrasing). The credit is
the publisher's logo in the source row. With nothing new, or when the rewrite fails a check, only the credit is added.

Invented facts cannot get through: the prompt allows only facts from our summary (written from the first report) and the
new report's own text; every number and every name in the rewrite must appear in one of them, every number our summary
already gives must stay, and stock phrases are refused, or nothing changes. The site keeps the summary before each
rewrite and the facts each report added (audit), and never changes the story's dates.

This runs here, not on the site, because the workers hold the new report's own text (the site keeps only our summary of
it) and already rotate the keys, cool down rate-limited ones and drop dead ones.
"""

from __future__ import annotations

import json
import logging
import re

from . import event_dates
from .summarise import MAX_QUOTE_WORDS, longest_copied_run, longest_quote_words, note_failure, note_success, \
    sentence_count, usable

log = logging.getLogger("aw_workers.merge")

MAX_WORDS = 50  # the facts line kept for the audit
SUMMARY_MAX_WORDS = 90  # the site holds summaries of 15-90 words

PROMPT = """You edit news summaries for Animal Writes, an Indian animal-welfare site.

OUR SUMMARY of an event:
{ours}

A NEW REPORT of the same event:
Title: {title}
Text: {text}

1. adds: the facts the NEW REPORT states that OUR SUMMARY does not (a name, a number, a place, a date, what an
   official, a court or the police did), in one plain line. Empty when it adds nothing that matters.
2. summary: when adds is not empty, OUR SUMMARY rewritten so those facts sit naturally in it. One summary, as a good
   news editor would write it: clear and specific, most important fact first.

Rules for both:
- Use only facts written in OUR SUMMARY and in the NEW REPORT title and text. Never add anything from memory or from
  elsewhere, and never guess. Keep every fact of OUR SUMMARY.
- Plain facts in your own words: who, what, where, when. No opinion, praise, advice or calls to action.
- Give a date only when the NEW REPORT text says the event happened on that date. Never give the day a report was
  published as the event date{pub_note}. OUR SUMMARY may wrongly give such a publication date as the event date: leave it
  out unless the NEW REPORT says the event happened on it.
- Do not name any newspaper, channel or website, and do not mention the reports ("the report says").
- summary: {min_s} to {max_s} sentences, at most {max_words} words, plain text with no heading, list or bold.
  Vary sentence length. Never quote more than {max_quote} words. No filler and no stock phrases such as
  "in a significant development", "it is worth noting", "furthermore", "moreover", "additionally", "underscores",
  "amid", "highlights", "pivotal", "visionary" or "a testament to". No praise words.
Answer with JSON only: {{"adds": "<the new facts, or empty>", "summary": "<the rewritten summary, or empty>"}}"""

# Stock phrasing that reads as generated text (owner, 15 Oct 2026: no AI content label).
STOCK = re.compile(
    r"\b(?:in a (?:significant|major|notable) development|(?:it is )?worth noting|it should be noted|furthermore|moreover|"
    r"additionally|underscor\w*|amid(?:st)?|notably|in conclusion|highlight(?:s|ed|ing)?|crucial role|pivotal|visionary|renowned|"
    r"remarkable|innovative|cutting-edge|world-class|state-of-the-art|landmark|"
    r"testament|delv\w*|landscape|paving the way|paves the way|shed(?:s|ding)? light|in a bid to|a move that|"
    r"this comes as|groundbreaking|commendable|robust|comprehensive|boasts?|serves as a reminder|in today's)\b",
    re.I,
)

# Sentence-opening words that need not appear in the sources (they are not facts).
_OPENERS = set("""a an the it its he she they his her their this that these those there here in on at by for from with
after before during while when according also both each officials authorities police residents locals""".split())


def _norm_num(s: str) -> str:
    return s.replace(",", "").rstrip(".")


def _numbers(text: str) -> set[str]:
    return {_norm_num(n) for n in re.findall(r"\d[\d,.]*", text or "")}


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[\w'’-]+", (text or "").lower()))


def _unsourced(written: str, source: str) -> list[str]:
    """Numbers and capitalised words (names, places, bodies, months) in `written` that `source` does not contain."""
    problems = []
    missing = sorted(_numbers(written) - _numbers(source))
    if missing:
        problems.append("numbers not in the reports: " + ", ".join(missing))
    have = _tokens(source)
    names = []
    for sentence in re.split(r"(?<=[.!?])\s+", written.strip()):
        for i, w in enumerate(re.findall(r"[\w'’-]+", sentence)):
            if not w[:1].isupper():
                continue
            low = w.lower()
            if i == 0 and low in _OPENERS:
                continue
            if low not in have and low.rstrip("'’s") not in have:
                names.append(w)
    if names:
        problems.append("names not in the reports: " + ", ".join(dict.fromkeys(names)))
    return problems


def check(adds: str, title: str, text: str) -> list[str]:
    """Why the facts line cannot be used ([] = it can): every number and name must be in the new report."""
    source = f"{title} {text}"
    problems = _unsourced(adds, source)
    n = sentence_count(adds)
    if n > 2:
        problems.append(f"needs 1-2 sentences, got {n}")
    if len(re.findall(r"[\w'’-]+", adds)) > MAX_WORDS:
        problems.append(f"over {MAX_WORDS} words")
    if longest_quote_words(adds) > MAX_QUOTE_WORDS or longest_copied_run(adds, source) > MAX_QUOTE_WORDS:
        problems.append(f"copies more than {MAX_QUOTE_WORDS} words from the report")
    return problems


def check_summary(summary: str, ours: str, title: str, text: str, publisher: str = "", pub_dates=None) -> list[str]:
    """Why the rewritten summary cannot replace ours ([] = it can). A publication date our summary gives as the event
    date is not a fact to keep (owner, 15 Oct 2026); the rewrite may not give one either, unless the new report says the
    event happened on it."""
    problems = _unsourced(summary, f"{ours} {title} {text}")
    pubs = _pubs(pub_dates)
    wrong = event_dates.pub_date_mentions(summary, pubs, f"{title}. {text}")
    if wrong:
        problems.append(event_dates.problem(wrong))
    lost = sorted(_numbers(event_dates.without_pub_dates(ours, pubs)) - _numbers(summary))
    if lost:
        problems.append("numbers from our summary left out: " + ", ".join(lost))
    n = sentence_count(summary)
    if not 2 <= n <= 4:
        problems.append(f"needs 2-4 sentences, got {n}")
    words = len(re.findall(r"[\w'’-]+", summary))
    if words > SUMMARY_MAX_WORDS or words < 15:
        problems.append(f"needs 15-{SUMMARY_MAX_WORDS} words, got {words}")
    stock = sorted({m.group(0).lower() for m in STOCK.finditer(summary)})
    if stock:
        problems.append("stock phrases: " + ", ".join(stock))
    if re.search(r"(^|\n)\s*(#|[-*•]\s|\*\*)|\*\*|\n", summary):
        problems.append("plain text only: no heading, list, bold or line break")
    if publisher and publisher.strip() and publisher.strip().lower() in summary.lower():
        problems.append("names the publisher")
    if longest_quote_words(summary) > MAX_QUOTE_WORDS or longest_copied_run(summary, f"{title} {text}") > MAX_QUOTE_WORDS:
        problems.append(f"copies more than {MAX_QUOTE_WORDS} words from the new report")
    if re.sub(r"\W+", "", summary.lower()) == re.sub(r"\W+", "", ours.lower()):
        problems.append("unchanged")
    return problems


def parse(reply: str) -> tuple[str, str] | None:
    """(adds, summary), or None when the reply holds no usable JSON."""
    m = re.search(r"\{.*\}", reply or "", re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict) or "adds" not in data:
        return None

    def clean(v) -> str:
        v = re.sub(r"\s+", " ", str(v or "")).strip()
        return "" if v.lower() in ("", "none", "nothing", "n/a", "empty") else v

    adds, summary = clean(data.get("adds")), clean(data.get("summary"))
    return (adds, summary) if adds and summary else ("", "")


def _pubs(pub_dates) -> list:
    return [d for d in (event_dates.to_date(p) for p in (pub_dates or [])) if d]


def prompt_for(ours: str, title: str, text: str, pub_dates=None) -> str:
    pubs = _pubs(pub_dates)
    note = (" (the reports came out on " + " and ".join(sorted({d.strftime("%-d %B %Y") for d in pubs})) + ")") if pubs else ""
    return PROMPT.format(ours=ours.strip() or "(nothing yet)", title=title.strip(), text=re.sub(r"\s+", " ", text)[:4000],
                         max_words=SUMMARY_MAX_WORDS, max_quote=MAX_QUOTE_WORDS, min_s=2, max_s=4, pub_note=note)


def _problems(got: tuple[str, str], ours: str, title: str, text: str, publisher: str, pub_dates=None) -> list[str]:
    adds, summary = got
    return check(adds, title, text) + check_summary(summary, ours, title, text, publisher, pub_dates) if adds else []


def rewrite(summariser, ours: str, title: str, text: str, publisher: str = "", pub_dates=None) -> tuple[str, str, str]:
    """(the facts the report adds, our summary rewritten with them, the model); ('', '', '') = credit only."""
    text = (text or "").strip()
    backends = list(getattr(summariser, "backends", []) or [])
    if not text or not ours.strip() or not backends:
        return "", "", ""
    prompt = prompt_for(ours, title, text, pub_dates)
    for i, backend in enumerate(backends):
        if not usable(summariser, i):
            continue
        try:
            got = parse(backend.ask(prompt))
            problems = _problems(got, ours, title, text, publisher, pub_dates) if got else []
            if problems:
                got = parse(backend.ask(prompt + "\n\nYour previous answer broke these rules: " + "; ".join(problems)
                                        + f"\nPrevious answer: {json.dumps({'adds': got[0], 'summary': got[1]}, ensure_ascii=False)}"
                                        + "\nAnswer again, using only facts from OUR SUMMARY and the NEW REPORT."))
                again = _problems(got, ours, title, text, publisher, pub_dates) if got else []
                if again:
                    log.info("merge: rewrite refused (%s); credit only", "; ".join(again))
                    return "", "", ""
        except Exception as exc:
            note_failure(summariser, i, exc)
            continue
        note_success(summariser, i)
        if got is None:
            continue  # no JSON: the next provider
        adds, summary = got
        return (adds, summary, f"{backend.name}:{getattr(backend, 'model', '')}") if adds else ("", "", "")
    return "", "", ""


def payload(keep_id: int, *, url: str, publisher: str, adds: str, summary: str, model: str, dup_id: int = 0) -> dict:
    body = {"keep_id": int(keep_id), "source_url": url, "publisher": publisher, "added_text": adds,
            "summary": summary if adds else "", "model": model if adds else ""}
    if dup_id:
        body["dup_id"] = int(dup_id)
    return body


def fold(summariser, client, keep_id, *, title: str, url: str, publisher: str, text: str, dup_id: int = 0,
         dry_run: bool = False, redo: bool = False, date=None) -> dict | None:
    """Add a same-event report to the first story: its credit, and (with new facts) the rewritten summary.
    `redo` folds a report already folded once again (after the rule changed). Never raises: a failure leaves the story
    as it was (the report is already held or rejected)."""
    if not keep_id or client is None or not hasattr(client, "same_story"):
        return None
    try:
        story = client.story(int(keep_id))
        done = {a.get("url") for a in story.get("additions") or [] if isinstance(a, dict)}
        ours = (story.get("summary") or "").strip()
        adds, summary, model = ("", "", "") if url in done and not redo else rewrite(summariser, ours, title, text, publisher,
                                                       [story.get("source_date"), story.get("date"), date])
        body = payload(int(keep_id), url=url, publisher=publisher, adds=adds, summary=summary, model=model, dup_id=dup_id)
        if dry_run:
            log.info("DRY_RUN would fold into #%s: %s", keep_id, json.dumps(body, ensure_ascii=False))
            return dict(body, summary_before=ours)
        client.same_story(body)
        log.info("merge: %s into #%s (%s)", url, keep_id, "summary rewritten" if adds else "credit only")
        return dict(body, summary_before=ours)
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
