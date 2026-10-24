"""News headlines and press coverage titles in sentence case (owner, 20 Oct 2026). Case only: never a word added,
removed or changed.

- A News title in Title Case ("Yogi Government Links Gaushalas With Natural Farming") is sent to the provider chain
  (summarise.Summariser backends) to be written in sentence case: a capital first letter, and capitals kept for proper
  nouns, places, organisations, acronyms (UP, BMC, HC, AWBI, PETA India) and people's names, as written.
- The answer is used only when it is the same string ignoring letter case (`case_only`). Anything else: the original
  title is kept and the item is flagged (the site stores the reason in aw_title_case_flag).
- Titles already in sentence case are not sent. Library titles are never touched (official document names).
- Lead-ins such as "Watch:", "Video:", "VIDEO |" and "Video of …" are only listed (`lead_in`), never removed here:
  the owner decides.

Ingest (run.py) cases each new News title; every press item sent to the site is cased the same way (ingest.Client.post
with press_caser). The one-off backfill writes a CSV for the owner to review first:
    python -m aw_workers.headline_case out.csv [--type aw_press]   # dry run: id, before, after, status, lead_in, without_lead_in
The site applies a reviewed CSV itself (wp aw news-titles apply [--type=aw_press]), title only; slugs and URLs never change.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import re
import sys

from . import counts as run_counts, summarise

log = logging.getLogger("aw_workers.headline_case")

BATCH = int(os.environ.get("AW_CASE_BATCH") or 20)

PROMPT = """Rewrite each Indian news headline below in sentence case. Change ONLY letter case: keep every word, number,
space and punctuation mark exactly as it is, in the same order.
Sentence case: capital first letter; every other word lower case EXCEPT proper nouns, which keep their capitals as
written: names of people, places (cities, districts, states, countries, rivers, parks), organisations, courts,
government bodies and schemes, brands, and acronyms (UP, BMC, HC, SC, AWBI, PETA India, FIR, NGO, NHAI, WII).
The first word after a colon stays as it is if it is a proper noun, otherwise lower case.

Headlines (JSON lines, id and title):
{items}

Answer with JSON lines only, one per headline, in the same order: {{"id": <id>, "title": "<the headline in sentence case>"}}"""

_LEAD = re.compile(r"^\s*((?:watch(?:\s+here)?|videos?|viral\s+video|in\s+pics|photos?|pics)\s*[:|\-–—]\s*)", re.I)
_LEAD_OF = re.compile(r"^\s*((?:viral\s+)?video\s+of)\b", re.I)


def case_only(before: str, after: str) -> bool:
    """True when `after` differs from `before` in letter case only (string equality ignoring case)."""
    return bool(after) and len(after) == len(before) and after.lower() == before.lower()


def needs_case(title: str) -> bool:
    """A title worth sending: in Title Case, i.e. most of its longer words after the first are capitalised (the first
    word is always capitalised, so it does not count)."""
    words = re.findall(r"[^\W\d_]{4,}", title or "")[1:]
    return len(words) >= 2 and sum(1 for w in words if w[0].isupper()) / len(words) >= 0.6


def lead_in(title: str) -> tuple[str, str]:
    """(the lead-in, the title without it) for "Watch:", "Video:", "VIDEO |" …; ("Video of", "") when removing it would
    need other words changed; ("", "") for none. Listed for the owner only."""
    m = _LEAD.match(title or "")
    if m:
        rest = title[m.end():].strip()
        return m[1].strip(), (rest[:1].upper() + rest[1:]) if rest else ""
    m = _LEAD_OF.match(title or "")
    return (m[1], "") if m else ("", "")


def parse(reply: str) -> dict[int, str]:
    out: dict[int, str] = {}
    for line in (reply or "").splitlines():
        line = line.strip().strip(",")
        if not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
            out[int(row["id"])] = str(row["title"])
        except (ValueError, KeyError, TypeError):
            continue
    return out


def ask(owner: summarise.Summariser, items: list[dict]) -> dict[int, str]:
    """One call for a batch [{id, title}], trying each usable key in turn (rate-limited keys rest, then are used again)."""
    prompt = PROMPT.format(items="\n".join(json.dumps({"id": it["id"], "title": it["title"]}, ensure_ascii=False) for it in items))
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


def decide(before: str, got: str | None) -> tuple[str, str]:
    """(title to use, status): the AI's title when only case changed, else the original with the reason."""
    if not needs_case(before):
        return before, "already sentence case"
    if not got:
        return before, "flag: no answer"
    got = got.strip()
    if not case_only(before, got):
        return before, "flag: words changed"
    if got == before:
        return before, "unchanged"
    if not got[:1].isupper() and before[:1].isupper():
        return before, "flag: first letter lower case"
    return got, "case changed"


def case_title(owner, title: str) -> tuple[str, str]:
    """Ingest: (title, flag). flag is '' when the title is fine (cased or already in sentence case)."""
    if not needs_case(title) or not getattr(owner, "backends", None):
        return title, ""
    new, status = decide(title, ask(owner, [{"id": 1, "title": title}]).get(1))
    return new, status[6:] if status.startswith("flag: ") else ""


def press_caser(env=os.environ):
    """A title caser for ingest.Client.title_caser (press items), or None without a provider key."""
    backends = summarise.backends_from_env(env, ledger={})
    if not backends:
        return None
    owner = summarise.Summariser(backends)
    return lambda title: case_title(owner, title)


def dry_run(owner, rows: list[dict]) -> list[dict]:
    """[{id, before, after, status, lead_in, without_lead_in}] for every row; nothing is written to the site."""
    send = [r for r in rows if needs_case(r["title"])]
    got: dict[int, str] = {}
    for start in range(0, len(send), BATCH):
        batch = send[start:start + BATCH]
        got.update(ask(owner, batch))
        log.info("cased %d of %d", min(start + BATCH, len(send)), len(send))
    out = []
    for r in rows:
        after, status = decide(r["title"], got.get(int(r["id"])))
        lead, rest = lead_in(r["title"])
        if after != r["title"] or status.startswith("flag") or lead:
            out.append({"id": r["id"], "before": r["title"], "after": after, "status": status, "lead_in": lead,
                        "without_lead_in": decide_lead(rest, after, lead)})
    return out


def decide_lead(rest: str, after: str, lead: str) -> str:
    """The title without its lead-in, in the case of the cased title ('' when it would need rewording)."""
    if not lead or not rest:
        return "needs rewording" if lead else ""
    m = _LEAD.match(after)
    if m:
        tail = after[m.end():].strip()
        return tail[:1].upper() + tail[1:]
    return rest


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    from . import ingest

    post_type = "aw_news"
    if "--type" in argv:
        i = argv.index("--type")
        post_type = argv[i + 1] if i + 1 < len(argv) else ""
        argv = argv[:i] + argv[i + 2:]
    if post_type not in ("aw_news", "aw_press"):
        log.error("--type is aw_news or aw_press")
        return 1
    out_path = argv[0] if argv else ("news-title-case.csv" if post_type == "aw_news" else "press-title-case.csv")
    client = ingest.Client(os.environ["WP_BASE_URL"], os.environ["WP_INGEST_USER"], os.environ["WP_INGEST_APP_PASSWORD"])
    rows, page = [], 1
    while True:
        data = client.seo_items(post_type, page, 200, missing_only=False)
        rows += [{"id": int(r["id"]), "title": r.get("title") or ""} for r in data.get("items") or []]
        if page >= int(data.get("pages") or 1):
            break
        page += 1
    backends = summarise.backends_from_env(os.environ, ledger={})
    if not backends:
        log.error("no provider key set")
        return 1
    out = dry_run(summarise.Summariser(backends), rows)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["id", "before", "after", "status", "lead_in", "without_lead_in"])
        w.writeheader()
        w.writerows(out)
    counts: dict[str, int] = {}
    for r in out:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    log.info("%d %s titles read; %d rows written to %s: %s", len(rows), post_type, len(out), out_path, counts)
    run_counts.write(read=len(rows), **{k: v for k, v in counts.items() if k})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
