"""Focus keywords for Rank Math (owner, 14 Oct 2026: Rank Math is the one SEO system).

Every published News, Library, Resources, Press, Opportunity, Campaign and Page item without a focus keyword gets a
short search phrase (2–4 words, lower case, English, built from words of its title) written into Rank Math's field
rank_math_focus_keyword. Listings get their organisation name on the site itself (aw-seo-extras), so they are not
asked here.

Items are read from aw/v1/seo-items (only those still without a keyword) and written through aw/v1/seo-focus, which
writes only where the field is still empty and applies the same rules as clean() below (aw_seo_clean_keyword on the
site). Twenty items go in one AI call; items whose phrase breaks a rule are asked once more, with the reason. When no provider answers, the items stay empty and the next run picks them up (the job is
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
For each item below, write the phrase of 2 to 4 words (never more) a person in India would type into Google to find that page.
Rules: lower case, English (also for a title in Hindi or Hinglish), no quotes or hashtags, no dates unless essential.
Use words from the item's title or its "about" text. Prefer the specific thing (species, place, law, court, organisation)
over generic words: "pune dog shelter", not "animal welfare". Never end on a connector such as "for", "of", "in", "to".
Do not use the site name "Animal Writes" unless the title itself is about Animal Writes.
Output only JSON: a list of objects {{"id": <id>, "keyword": "<phrase>"}}, one per item, nothing else.

{items}"""

RETRY = """These phrases were refused. Write a new 2 to 4 word phrase for each item, following the same rules.
{reasons}"""

# Words a phrase must not end on (the site's AW_SEO_DANGLING list).
DANGLING = set("""& + and or but nor of the a an to in on at by for with from into onto over under after before amid as via
per than that which who whose its their his her is are was were be has have had will can against between near about
across during without within vs v every each all some any this these those our your my what how why when where whether
if not also so very more most such both says said""".split())

# Words that alone make no search phrase (the site's AW_SEO_GENERIC_WORDS).
GENERIC = {"animal", "animals", "welfare", "india", "indian", "save", "help", "news", "care", "update", "updates",
           "issue", "case", "story"}

_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def _words(text: str) -> set[str]:
    return {w[:5] for w in _WORD.findall((text or "").lower()) if len(w) >= 3}


def why_refused(keyword: str, title: str, text: str = "") -> str:
    """'' when the phrase is usable, else the rule it breaks (the site checks the same rules)."""
    kw = _normalise(keyword)
    words = kw.split(" ")
    if not kw or not 2 <= len(words) <= 4 or len(kw) > 60:
        return "not 2 to 4 words"
    t = (title or "").lower()
    if "animal writes" in kw and "animal writes" not in t and "animalwrites" not in t:
        return "uses the site name"
    if words[-1] in DANGLING:
        return f'ends on "{words[-1]}"'
    if set(words) <= GENERIC:
        return "only generic words"
    if not _words(kw) & _words(f"{title} {text}"):
        return "shares no word with the title or text"
    return ""


def _normalise(keyword: str) -> str:
    kw = re.sub(r"[\"“”‘’`#]+", "", str(keyword or "")).lower()
    return re.sub(r"\s+", " ", kw).strip(" \t\n.,;:'-")


def clean(keyword: str, title: str, text: str = "") -> str:
    """The phrase as stored, or '' when it breaks a rule."""
    return "" if why_refused(keyword, title, text) else _normalise(keyword)


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


def _lines(items: list[dict]) -> str:
    return "\n".join(json.dumps({"id": it["id"], "title": it["title"], "about": (it.get("excerpt") or "")[:300]},
                                ensure_ascii=False) for it in items)


def ask(owner: summarise.Summariser, items: list[dict], refused: dict[int, str] | None = None) -> dict[int, str]:
    """One call for a batch, trying each usable key in turn (rate-limited keys rest, then are used again).
    With `refused` ({id: reason}), the call asks again for those items, giving the reason."""
    prompt = PROMPT.format(items=_lines(items))
    if refused:
        prompt += "\n\n" + RETRY.format(reasons="\n".join(f"id {i}: {r}" for i, r in refused.items()))
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


def _usable(batch: list[dict], got: dict[int, str]) -> tuple[list[dict], dict[int, str]]:
    """([{id, keyword}] that pass the rules, {id: reason} for the rest of the batch)."""
    good, refused = [], {}
    for r in batch:
        kw = got.get(r["id"], "")
        why = why_refused(kw, r["title"], r.get("excerpt") or "") if kw else "no phrase"
        if why:
            refused[r["id"]] = f'"{kw}" {why}' if kw else why
        else:
            good.append({"id": r["id"], "keyword": _normalise(kw)})
    return good, refused


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
            items, refused = _usable(batch, got)
            if refused:  # once more, with the reason
                again = [r for r in batch if r["id"] in refused]
                more, refused = _usable(again, ask(owner, again, refused))
                items += more
            stats["unusable"] += len(refused)
            if items:
                res = client.seo_focus(items)
                stats["written"] += int(res.get("written") or 0)
                stats["refused by site"] += len(res.get("skipped") or [])
    log.info("done: %s; providers used: %s", dict(stats), owner.used)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
