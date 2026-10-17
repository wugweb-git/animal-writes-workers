"""Rewrite every published summary that breaks the current Summary rules (owner, 18 Oct 2026).

  python -m aw_workers.summary_rewrite        (env: WP_*, the AI keys; BATCH=25, PAUSE_S=4, DRY_RUN, AW_IDS=1,2,3)

One batch per run, newest first. The site (aw/v1/summary-rewrite) lists the summaries not yet settled and keeps the
state, so a rerun skips what is done. For each item that breaks the rules:
1. the source page (or PDF) is read again and the summary written from it and the current summary's facts;
2. below the section's minimum, or the page is thin or unreadable: other coverage of the same event is found (Google
   News search with the key names, place and dates) and the summary is written again from all of it;
3. every candidate passes the same checks as new summaries (summary_rules.problems), plus: every number and name must
   be in the current summary, the source or the other coverage; every number of the current summary stays; the
   publication date is never given as the event date;
4. the best candidate that follows the rules is written (old text to aw_summary_history, the story date never moves,
   search description refreshed, only that item's cache purged). If the minimum cannot be reached without padding,
   the best accurate version is written and the item is listed as 'short'. Nothing is held for review.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from datetime import date, datetime

import requests

from . import coverage, event_dates, ingest, merge, summarise, summary_dates, summary_rules
from .summarise import note_failure, note_success, usable, wait_for_cooldown

log = logging.getLogger("aw_workers.summary_rewrite")

SECTION = {"aw_news": "news", "aw_legal": "library", "post": "research"}

PROMPT = """You rewrite one summary for Animal Writes, an Indian animal-welfare site. This one is for its {label} section.

CURRENT SUMMARY (its facts were checked against the source; keep every fact and every number in it):
{ours}

What is wrong with it: {why}

SOURCE ({publisher}), title: {title}
Published: {pub} (the day the report came out, not the date of the event)
Source text: {text}

Write the summary again from the CURRENT SUMMARY and the SOURCE text.
- Use only facts written in the CURRENT SUMMARY or the SOURCE text (including any other report of the same event that
  is marked in it). Never add anything from memory, never guess, never pad to reach a length: if the facts are few,
  write fewer words.
- Keep every number of the current summary. Write any date as day, three-letter month, year (for example 3 Oct 2026).
- Never give the publication date as the date of the event.
- Do not mention the publisher or "the report"; do not name other publishers.
{rules}
- Output only the summary text, nothing else."""

_MONTHS = ("January February March April May June July August September October November December "
           "Jan Feb Mar Apr Jun Jul Aug Sep Sept Oct Nov Dec")


def one_date_format(s: str) -> str:
    """Every written date as j M Y (the site's one date format)."""
    m = "January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec"

    def mo(x):
        return x[:3].capitalize()

    s = re.sub(r"\b(\d{1,2})(?:st|nd|rd|th)?(?:\s+day\s+of)?\s+(" + m + r")\.?,?\s+(\d{4})\b",
               lambda x: f"{int(x[1])} {mo(x[2])} {x[3]}", s)
    return re.sub(r"\b(" + m + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b",
                  lambda x: f"{int(x[2])} {mo(x[1])} {x[3]}", s)


def clean(reply: str) -> str:
    t = (reply or "").strip()
    t = re.sub(r"^(?:here is|here's)[^\n:]*:\s*", "", t, flags=re.I)
    t = re.sub(r"^\**summary\**:?\s*", "", t, flags=re.I)
    t = t.strip().strip('"“”').strip()
    t = re.sub(r"[*_#`]+", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return one_date_format(t)


def _pubs(row: dict, page_pub: list[str]) -> list:
    return [d for d in (event_dates.to_date(x) for x in [row.get("source_date"), row.get("date"), *page_pub]) if d]


def evaluate(summary: str, ours: str, row: dict, text: str, section: str, pubs) -> tuple[list[str], list[str]]:
    """(hard problems, 'too short' problems). Hard ones block publishing; short ones are never fixed by padding."""
    title = row.get("title") or ""
    rules = summary_rules.current()
    names = " ".join(summary_rules.entities_in(f"{ours} {title} {text}", rules))
    hard = merge._unsourced(summary, f"{ours} {title} {text} {names} {_MONTHS}")
    wrong = event_dates.pub_date_mentions(summary, pubs, f"{title}. {text}") if pubs else []
    if wrong:
        hard.append(event_dates.problem(wrong))
    lost = sorted(merge._numbers(event_dates.without_pub_dates(ours, pubs)) - merge._numbers(summary))
    if lost:
        hard.append("numbers from the current summary left out: " + ", ".join(lost))
    found = summary_rules.problems(summary, section, f"{title}. {text}", rules, publisher=row.get("publisher") or "")
    hard += [p for p in found if not p.startswith(summary_rules.SHORT)]
    have = merge._tokens(f"{ours} {title} {text}")
    lost_apos = sorted({w for w in merge._tokens(summary) if w.endswith("s") and w not in have
                        and (w[:-1] + "’s" in have or w[:-1] + "'s" in have)})
    if lost_apos:
        hard.append("apostrophe lost in: " + ", ".join(lost_apos))
    return hard, [p for p in found if p.startswith(summary_rules.SHORT)]


def read_source(url: str, session=None) -> dict:
    """{text, published, status} of the source: a page, a PDF, or a Google News link resolved to its publisher."""
    from .enrich import UA, resolve_google_news

    if not url:
        return {"text": "", "published": [], "status": "no url"}
    url = resolve_google_news(url)
    if url.lower().split("?")[0].endswith(".pdf"):
        try:
            r = requests.get(url, headers=UA, timeout=40)
            r.raise_for_status()
            if len(r.content) > 15_000_000:
                return {"text": "", "published": [], "status": "pdf too large"}
            from .documents import pdf_text

            return {"text": re.sub(r"\s+", " ", pdf_text(r.content, max_pages=12))[:20000], "published": [], "status": "pdf"}
        except Exception as exc:  # noqa: BLE001
            return {"text": "", "published": [], "status": type(exc).__name__}
    return summary_dates.read_page(url, session)


class Rewriter:
    def __init__(self, summariser, pause: float = 4.0):
        self.s = summariser
        self.pause = pause
        self.model = ""

    def _ask(self, prompt: str, check) -> tuple[str, list[str], list[str]] | None:
        """Best answer over the providers: (summary, hard, short). One corrective retry per provider."""
        best = None
        for _ in range(2):  # a second pass once a rate-limited key has rested
            for i, backend in enumerate(self.s.backends):
                if not usable(self.s, i):
                    continue
                try:
                    cand = clean(backend.ask(prompt))
                    hard, short = check(cand)
                    if hard:
                        cand2 = clean(backend.ask(prompt + "\n\nYour previous answer broke these rules: " + "; ".join(hard)
                                                  + f"\nPrevious answer: {cand}\nWrite it again, fixing exactly these."))
                        h2, s2 = check(cand2)
                        if len(h2) <= len(hard):
                            cand, hard, short = cand2, h2, s2
                except Exception as exc:  # noqa: BLE001
                    note_failure(self.s, i, exc)
                    log.info("provider %s failed: %s", backend.name, str(exc)[:120])
                    continue
                note_success(self.s, i)
                self.model = f"{backend.name}:{getattr(backend, 'model', '')}"
                result = (cand, hard, short)
                if not hard:
                    return result
                if best is None or len(hard) < len(best[1]):
                    best = result
            if best is not None or not wait_for_cooldown(self.s):
                break
        return best

    def candidate(self, row: dict, section: str, text: str, pubs, why: str) -> tuple[str, list[str], list[str]] | None:
        rules = summary_rules.current()
        ours = row.get("summary") or ""
        prompt = PROMPT.format(
            label=rules["sections"][section]["label"], ours=ours or "(none: the summary is empty)", why=why or "check against the rules",
            publisher=row.get("publisher") or "the publisher", title=row.get("title") or "",
            pub=", ".join(sorted({d.isoformat() for d in pubs})) or "unknown",
            text=re.sub(r"\s+", " ", text)[:9000] or "(the source could not be read; use the current summary's facts only)",
            rules=summary_rules.rules_block(section, rules, f"{ours} {row.get('title') or ''} {text}"))
        check = lambda c: evaluate(c, ours, row, text, section, pubs)  # noqa: E731
        res = self._ask(prompt, check)
        sec = rules["sections"][section]
        if res and not res[1] and res[2] and len(summary_rules.words(text)) >= 2.5 * int(sec["min_words"]):
            # the source holds more than the answer used: ask once for the further facts it states (never padding)
            more = self._ask(prompt + f"\n\nYour previous answer was too short ({len(summary_rules.words(res[0]))} words; "
                             f"{sec['min_words']} to {sec['max_words']} are expected) but the source text holds more facts. Add the "
                             "further facts it states (who, where, when, numbers, what the court or body directed, what happens next). "
                             f"Never pad.\nPrevious answer: {res[0]}", check)
            if more and not more[1] and len(summary_rules.words(more[0])) > len(summary_rules.words(res[0])):
                res = more
        return res


def process(rw: Rewriter, row: dict) -> dict:
    """Decide one item. Returns {action: write|settle|failed, summary, state, model, also, reason, ...}."""
    section = row.get("section") or SECTION.get(row["type"], "news")
    ours = row.get("summary") or ""
    rules = summary_rules.current()
    cur = summary_rules.problems(ours, section, "", rules, publisher=row.get("publisher") or "")
    why = "; ".join(cur) or "rewrite"
    page = read_source(row.get("source_url") or "")
    text = page.get("text") or ""
    pubs = _pubs(row, page.get("published") or [])
    cands: list[tuple[str, list[str], list[str], list]] = []
    info = {"src": f"{page.get('status')}:{len(text)}", "cov": 0}
    rw.info = info
    first = rw.candidate(row, section, text, pubs, why) if (text or ours) else None
    if first:
        cands.append((*first, []))
    need_more = not first or first[1] or first[2]  # a problem, or still short
    if need_more and row.get("title"):
        when = event_dates.to_date(row.get("source_date") or row.get("date"))
        try:
            reports = coverage.find(row["title"], row.get("source_url") or "", when, row.get("publisher") or "")
        except Exception as exc:  # noqa: BLE001
            log.info("coverage failed: %s", type(exc).__name__)
            reports = []
        info["cov"] = len(reports)
        if reports:
            wider = coverage.combined_text(text, reports)
            second = rw.candidate(row, section, wider, pubs, why)
            if second:
                cands.append((*second, [{"name": r.publisher, "url": r.url} for r in reports]))
    ok = [c for c in cands if not c[1]]
    rw.info = info
    if not ok:
        reason = "; ".join(cands[0][1])[:280] if cands else (
            "source unreadable, no other coverage found" if not (text or ours) else "no provider answered")
        return {"action": "failed", "reason": reason}
    meets = [c for c in ok if not c[2]]
    best = meets[0] if meets else max(ok, key=lambda c: len(summary_rules.words(c[0])))
    new, _, short, used_also = best
    ours_hard = [p for p in cur if not p.startswith(summary_rules.SHORT)]
    ours_w, new_w = len(summary_rules.words(ours)), len(summary_rules.words(new))
    if short:  # cannot reach the minimum without padding
        if not ours_hard and new_w <= ours_w:
            return {"action": "settle", "reason": "; ".join(short)[:280]}
        state = "short"
    else:
        state = "done"
    if new == ours:
        return {"action": "settle", "reason": "unchanged"}
    return {"action": "write", "summary": new, "state": state, "model": rw.model,
            "also": used_also, "reason": "; ".join(short)[:280], "before_words": ours_w, "after_words": new_w}


def candidates(client, batch: int, only: list[int] | None, dry: bool, log_) -> list[dict]:
    """The next `batch` items that break the rules, newest first; items that already follow them are settled."""
    out, ok_ids, skip = [], [], 0
    while len(out) < batch:
        got = client._call("GET", "/summary-rewrite", params=({"ids[]": only} if only else {"skip": skip, "per_page": 100}))
        for row in got.get("items") or []:
            section = row.get("section") or SECTION.get(row["type"], "news")
            probs = summary_rules.problems(row.get("summary") or "", section, "", publisher=row.get("publisher") or "")
            if not probs and not only:
                ok_ids.append(row["id"])
            elif row.get("tries", 0) < 2 or only:
                out.append(row)
            if len(out) >= batch:
                break
        if only or got.get("next") is None:
            break
        skip = got["next"]
    if ok_ids and not dry:
        for i in range(0, len(ok_ids), 200):
            client._call("POST", "/summary-rewrite", json={"ok": ok_ids[i:i + 200]})
    log_.info("%d items follow the rules (settled); %d to rewrite this run", len(ok_ids), len(out))
    return out[:batch]


def main(env=os.environ) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    dry = env.get("DRY_RUN", "false").lower() == "true"
    batch = int(env.get("BATCH") or 25)
    pause = float(env.get("PAUSE_S") or 4)
    only = [int(x) for x in re.split(r"[,\s]+", env.get("AW_IDS", "").strip()) if x] or None
    deadline = time.monotonic() + 60 * float(env.get("MAX_MINUTES") or 38)
    client = ingest.Client(env["WP_BASE_URL"], env["WP_INGEST_USER"], env["WP_INGEST_APP_PASSWORD"])
    summary_rules.use(client)
    backends = summarise.backends_from_env(env, ledger={})
    log.info("providers: %s", ", ".join(f"{b.name}:{getattr(b, 'model', '')}" for b in backends))
    rw = Rewriter(summarise.Summariser(backends), pause)
    rows = candidates(client, batch, only, dry, log)
    stats = {"done": 0, "short": 0, "settled": 0, "failed": 0, "error": 0}
    report = []
    for n, row in enumerate(rows):
        if time.monotonic() > deadline:
            log.info("time is up; the rest goes to the next run")
            break
        if n:
            time.sleep(pause)
        try:
            res = process(rw, row)
            line = {"id": row["id"], "section": row.get("section"), "action": res["action"], **getattr(rw, "info", {})}
            if res["action"] == "write":
                line.update(state=res["state"], before=row["summary"], after=res["summary"], model=res["model"],
                            words=[res["before_words"], res["after_words"]], also=[a["url"] for a in res["also"]], reason=res["reason"])
                if not dry:
                    client._call("POST", "/summary-rewrite", json={
                        "id": row["id"], "before_md5": row["md5"], "summary": res["summary"], "state": res["state"],
                        "model": res["model"], "reason": res["reason"], "also": res["also"]})
                stats[res["state"]] += 1
            elif res["action"] == "settle":
                line["reason"] = res["reason"]
                if not dry:
                    client._call("POST", "/summary-rewrite", json={"id": row["id"], "before_md5": row["md5"], "settle": res["reason"] or "kept"})
                stats["settled"] += 1
            else:
                line["reason"] = res["reason"]
                if not dry:
                    client._call("POST", "/summary-rewrite", json={"id": row["id"], "before_md5": row["md5"], "failed": res["reason"] or "failed"})
                stats["failed"] += 1
            report.append(line)
            print(json.dumps(line, ensure_ascii=False))
        except ingest.AuthError:
            raise
        except Exception as exc:  # noqa: BLE001
            stats["error"] += 1
            log.warning("#%s: %s: %s", row["id"], type(exc).__name__, str(exc)[:200])
    print("RUN", json.dumps(stats))
    if not dry:
        try:
            prog = client._call("GET", "/summary-rewrite", params={"progress": 1})
            print("PROGRESS", json.dumps(prog.get("counts")))
        except Exception as exc:  # noqa: BLE001
            log.info("progress not read: %s", type(exc).__name__)
    out = env.get("GITHUB_STEP_SUMMARY")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"### Summary rewrite\n`{json.dumps(stats)}`\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
