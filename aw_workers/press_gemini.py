"""One-off press search through Google Search, via Gemini's search grounding (owner, 10 Oct 2026: no Brave key;
"anything about Animal Writes ... even on insta/twitter etc").

Asks Google (through Gemini with the google_search tool) for coverage since the press floor on the open web, in Indian
languages and on each social platform; collects the source URLs Google returns (never the model's text). Own pages
(animalwrites.in, the Animal Writes social accounts) are skipped. News pages must mention Animal Writes or the letter
campaign in their text; social posts cannot be read without logging in, so they are sent for review as found.
Every hit goes to the site as a "Press: to review" draft; nothing is published automatically.

Run: python -m aw_workers.press_gemini   (DRY_RUN=true by default; needs GEMINI_API_KEY / GEMINI_API_KEY_2)
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.parse
from pathlib import Path

import requests
import yaml

from . import counts as run_counts, enrich, headline_case, ingest, keywords, press, relevance
from .run import truthy
from .summarise import _keys

log = logging.getLogger("aw_workers.press_gemini")
MODEL = os.environ.get("AW_PRESS_GEMINI_MODEL", "gemini-2.5-flash")
URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
BROWSER = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"}
OWN = re.compile(r"animalwrites\.in|facebook\.com/AnimalWritesIndia|instagram\.com/animalwrites_india|x\.com/AnimalWrites|twitter\.com/AnimalWrites", re.I)
SUBJECT = ('"Animal Writes" (animalwrites.in, #animalwrites), the citizen campaign in which people across India posted '
         'handwritten letters to the Supreme Court on 29 November 2025 about the stray dogs order')
WHERE = [
    ("news articles", ""), ("Hindi news coverage", ""), ("Tamil, Telugu, Kannada or Malayalam news coverage", ""),
    ("Marathi, Bengali or Gujarati news coverage", ""), ("TV news segments or podcasts", ""), ("blog posts or opinion pieces", ""),
    ("Instagram posts or reels", "instagram.com"), ("X (Twitter) posts", "x.com"), ("Facebook posts or videos", "facebook.com"),
    ("YouTube videos or shorts", "youtube.com"), ("LinkedIn posts", "linkedin.com"), ("Reddit threads", "reddit.com"),
    ("Threads posts", "threads.net"),
]
PERIODS = ["in November 2025", "in December 2025", "between January and March 2026", "between April and June 2026",
           "between July and October 2026"]


def web_keys(env) -> list[str]:
    """Gemini's Google search (free tier). OpenRouter's web search (marked "or:") only when AW_PRESS_OPENROUTER_MODEL
    names a free model: its old default (openai/gpt-4o-mini:online) was paid (owner, 25 Oct 2026: no paid models)."""
    model = env.get("AW_PRESS_OPENROUTER_MODEL") or ""
    free = model.split(":online")[0].endswith(":free")
    return (["or:" + k for k in _keys(env.get("OPENROUTER_API_KEY"))] if free else []) + _keys(env.get("GEMINI_API_KEY_2"), env.get("GEMINI_API_KEY"))


def ask(key: str, prompt: str) -> list[tuple[str, str]]:
    r = requests.post(URL.format(model=MODEL), headers={"x-goog-api-key": key}, timeout=120,
                      json={"contents": [{"parts": [{"text": prompt}]}], "tools": [{"google_search": {}}]})
    if r.status_code != 200:
        raise RuntimeError(f"{r.status_code}: {r.json().get('error', {}).get('message', '')[:120]}")
    meta = (r.json().get("candidates") or [{}])[0].get("groundingMetadata", {}) or {}
    return [(c["web"].get("uri", ""), c["web"].get("title", "")) for c in meta.get("groundingChunks", []) if c.get("web")]


def ask_openrouter(key: str, prompt: str) -> list[tuple[str, str]]:
    """OpenRouter's web search (":online" models): the cited URLs, never the model's text."""
    r = requests.post("https://openrouter.ai/api/v1/chat/completions", headers={"Authorization": f"Bearer {key}"}, timeout=180,
                      json={"model": os.environ["AW_PRESS_OPENROUTER_MODEL"],  # set and free, or web_keys never gives an "or:" key
                            "messages": [{"role": "user", "content": prompt}]})
    if r.status_code != 200:
        raise RuntimeError(f"{r.status_code}: {r.text[:120]}")
    msg = (r.json().get("choices") or [{}])[0].get("message", {}) or {}
    out = [((a.get("url_citation") or {}).get("url", ""), (a.get("url_citation") or {}).get("title", "")) for a in msg.get("annotations") or []]
    out += [(u, "") for u in re.findall(r"https?://[^\s)\]>\"']+", msg.get("content") or "")]
    return [(u.rstrip(".,"), t) for u, t in out if u]


def resolve(uri: str) -> str:
    """Google's grounding links redirect to the source."""
    try:
        return requests.get(uri, allow_redirects=False, timeout=20).headers.get("location", uri)
    except requests.RequestException:
        return uri


def page_facts(url: str) -> dict:
    """Title, date and whether the page mentions Animal Writes or the campaign (news pages only)."""
    try:
        r = requests.get(url, headers=BROWSER, timeout=20)
    except requests.RequestException:
        return {}
    if r.status_code >= 400:
        return {"status": r.status_code}
    html = r.text
    def meta(prop):
        m = re.search(r'<meta[^>]+(?:property|name)=["\']' + re.escape(prop) + r'["\'][^>]+content=["\']([^"\']+)', html, re.I)
        return m.group(1).strip() if m else ""
    text = re.sub(r"<[^>]+>", " ", html)
    return {"status": r.status_code, "title": meta("og:title"), "site": meta("og:site_name"),
            "date": (meta("article:published_time") or meta("datePublished") or "")[:10],
            "relevant": bool(press.RELEVANT.search(text))}


def collect(client, dry_run: bool, floor: str, periods: list[str], keys: list[str], gate=None) -> tuple[dict, list]:
    """Ask Google for each kind of coverage in each period; send every hit that passes the checks as a Press draft."""
    found: dict[str, str] = {}
    for what, site in WHERE:
        for period in periods:
            prompt = f"Search Google for {what} published {period} about {SUBJECT}." + (f" Search site:{site} only." if site else "") + \
                     " List every URL you find."
            for key in list(keys):
                try:
                    hits = ask_openrouter(key[3:], prompt) if key.startswith("or:") else ask(key, prompt)
                    for uri, title in hits:
                        found.setdefault(resolve(uri), title)
                    break
                except requests.RequestException as exc:  # a slow or dropped search: skip it, keep the key
                    log.warning("%s / %s: %s", what, period, exc)
                    break
                except RuntimeError as exc:
                    log.warning("%s / %s: %s", what, period, exc)
                    if any(c in str(exc) for c in ("402", "403", "404", "429")):
                        keys.remove(key)  # this key's quota is used up for today
            time.sleep(3)
            if not keys:
                break
    counts, rows = {"new": 0, "duplicate": 0, "failed": 0, "own page": 0, "not relevant": 0, "too old": 0}, []
    for url in found:
        url = press.normalise(url)
        if OWN.search(url):
            counts["own page"] += 1
            continue
        platform = press.platform_of(url)
        facts = {} if platform != "web" else page_facts(url)
        if platform == "web" and facts.get("status") == 200 and not facts.get("relevant"):
            counts["not relevant"] += 1
            continue
        if facts.get("date") and facts["date"] < floor:
            counts["too old"] += 1
            continue
        payload = {
            "type": "press", "title": (facts.get("title") or found.get(url) or url)[:200], "source_url": url,
            "source_name": facts.get("site") or urllib.parse.urlparse(url).netloc.removeprefix("www."),
            "source_date": facts.get("date", ""), "platform": platform, "format": press.format_of(url, platform),
            "account_handle": press.handle_of(url, platform), "image_url": enrich.og_image(url) if platform == "web" else "",
            "query": "google-search", "feed": "press-google-search",
        }
        if not press.gated(gate, payload, counts):
            continue
        if dry_run:
            log.info("DRY_RUN would POST press: %s", json.dumps(payload, ensure_ascii=False))
            counts["new"] += 1
        else:
            try:
                result = client.post(payload)
                counts["duplicate" if result == "duplicate" else "new"] += 1
            except Exception as exc:
                counts["failed"] += 1
                log.error("failed %s: %s", url, exc)
        rows.append({"date": payload["source_date"], "platform": platform, "source": payload["source_name"], "title": payload["title"], "url": url})
    return counts, rows


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    dry_run = truthy(os.environ.get("DRY_RUN"), default=True)
    keys = web_keys(os.environ)
    cfg = keywords.press_config(yaml.safe_load(Path("sources.yaml").read_text(encoding="utf-8")) or {})  # Press keywords, else v1
    floor = str(cfg.get("date_floor", "2025-11-01"))
    client = None if dry_run else ingest.Client(os.environ["WP_BASE_URL"], os.environ["WP_INGEST_USER"], os.environ["WP_INGEST_APP_PASSWORD"])
    if client:
        client.title_caser = headline_case.press_caser()  # sentence case, letter case only (owner, 20 Oct 2026)
    gate = relevance.gate_from_env(client, dry_run=dry_run)
    counts, rows = collect(client, dry_run, floor, PERIODS, keys, gate=gate)
    gate.save()
    Path("press-gemini-report.json").write_text(json.dumps({"counts": counts, "items": rows}, ensure_ascii=False, indent=1))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        lines = ["### Press via Google Search (Gemini)", "", f"{counts}", "", "| Date | Platform | Source | Title |", "|---|---|---|---|"]
        lines += [f"| {r['date']} | {r['platform']} | {r['source']} | [{r['title']}]({r['url']}) |" for r in rows]
        with open(summary, "a") as fh:
            fh.write("\n".join(lines) + "\n")
    print(json.dumps(counts))
    run_counts.write(**{k.replace(" ", "_"): v for k, v in counts.items()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
