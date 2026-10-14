"""Focus keywords for Rank Math (owner, 14 Oct 2026: Rank Math is the one SEO system).

Every published News, Library, Resources, Press, Opportunity, Campaign and Page item without a focus keyword gets a
short search phrase (2–4 words, lower case, English, built from words of its title) written into Rank Math's field
rank_math_focus_keyword. Listings get their organisation name on the site itself (aw-seo-extras), so they are not
asked here.

Items are read from aw/v1/seo-items (only those still without a keyword) and written through aw/v1/seo-focus, which
writes only where the field is still empty and refuses a phrase that shares no word with the title. Twenty items go
in one AI call. When no provider answers, the items stay empty and the next run picks them up (the job is
resumable and runs daily for new items).

Run: python -m aw_workers.focus_keywords   (needs WP_* secrets and provider keys)
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections import Counter

from . import ingest, summarise

log = logging.getLogger("aw_workers.focus_keywords")

TYPES = ["aw_news", "aw_legal", "post", "aw_press", "aw_opportunity", "aw_campaign", "page"]
BATCH = 20

PROMPT = """You choose SEO focus keywords for pages of an Indian animal-welfare website.
For each item below, write the 2 to 4 word phrase a person in India would type into Google to find that page.
Rules: lower case, English, no quotes, no site or brand name, no dates unless essential, and use words from the
item's title. Output only JSON: a list of objects {{"id": <id>, "keyword": "<phrase>"}}, one per item, nothing else.

{items}"""

_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def _words(text: str) -> set[str]:
    return {w[:5] for w in _WORD.findall((text or "").lower()) if len(w) >= 3}


def clean(keyword: str, title: str) -> str:
    """The phrase as stored, or '' when it breaks a rule (the site checks the same)."""
    kw = re.sub(r"[\"“”‘’`]+", "", str(keyword or "")).lower()
    kw = re.sub(r"\s+", " ", kw).strip(" \t\n.,;:'-")
    words = kw.split(" ")
    if not kw or len(words) > 6 or len(kw) > 70 or "animal writes" in kw:
        return ""
    return kw if _words(kw) & _words(title) else ""


def parse(reply: str) -> dict[int, str]:
    """{id: keyword} from the model's reply (a JSON list, possibly wrapped in prose or a code fence)."""
    m = re.search(r"\[.*\]", reply or "", re.S)
    if not m:
        return {}
    try:
        rows = json.loads(m.group(0))
    except ValueError:
        return {}
    out = {}
    for r in rows if isinstance(rows, list) else []:
        if isinstance(r, dict) and str(r.get("id", "")).isdigit() and r.get("keyword"):
            out[int(r["id"])] = str(r["keyword"])
    return out


def ask(owner: summarise.Summariser, items: list[dict]) -> dict[int, str]:
    """One call for a batch, trying each usable key in turn (rate-limited keys rest, then are used again)."""
    lines = "\n".join(json.dumps({"id": it["id"], "title": it["title"], "about": (it.get("excerpt") or "")[:300]},
                                 ensure_ascii=False) for it in items)
    prompt = PROMPT.format(items=lines)
    for _ in range(2):
        for i, backend in enumerate(owner.backends):
            if not summarise.usable(owner, i):
                continue
            try:
                reply = backend.ask(prompt)
            except Exception as exc:  # noqa: BLE001 - next key
                summarise.note_failure(owner, i, exc)
                continue
            got = parse(reply)
            if got:
                summarise.note_success(owner, i)
                owner.used[backend.name] = owner.used.get(backend.name, 0) + 1
                return got
        if not summarise.wait_for_cooldown(owner):
            break
    return {}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    client = ingest.Client(os.environ["WP_BASE_URL"], os.environ["WP_INGEST_USER"], os.environ["WP_INGEST_APP_PASSWORD"])
    backends = summarise.backends_from_env(os.environ, ledger={})
    if not backends:
        log.error("no provider key set: nothing done, the items stay without a keyword")
        return 0
    owner = summarise.Summariser(backends)
    types = [t.strip() for t in (os.environ.get("AW_SEO_TYPES") or ",".join(TYPES)).split(",") if t.strip()]
    stats = Counter()
    for t in types:
        rows, page = [], 1
        while True:
            data = client.seo_items(t, page, 200, missing_only=True)
            rows += [r for r in data.get("items") or [] if not r.get("has_focus")]
            if page >= int(data.get("pages") or 1):
                break
            page += 1
        log.info("%s: %d item(s) without a focus keyword", t, len(rows))
        for start in range(0, len(rows), BATCH):
            batch = rows[start:start + BATCH]
            got = ask(owner, batch)
            if not got:
                stats["no answer"] += len(batch)
                continue
            titles = {r["id"]: r["title"] for r in batch}
            items = [{"id": i, "keyword": k} for i, k in ((i, clean(k, titles.get(i, ""))) for i, k in got.items())
                     if i in titles and k]
            stats["unusable"] += len(batch) - len(items)
            if items:
                res = client.seo_focus(items)
                stats["written"] += int(res.get("written") or 0)
                stats["refused by site"] += len(res.get("skipped") or [])
    log.info("done: %s; providers used: %s", dict(stats), owner.used)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
