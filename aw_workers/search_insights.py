"""Search Insights, phase 3: rising terms from free sources (plan: docs/seo/search-insights-plan.md §5.3 in the
WordPress repository; owner, 24 Oct 2026).

Daily (08:10 IST): Google Trends "Trending now" RSS for India, kept only when a trending term or its news names one of
our keywords, topics or species; Wikipedia pageviews for a watchlist, flagged at 2x the 30-day average (a user-agent
with a contact, at most one request a second); GDELT DOC timelines for a topic watchlist, India sources, flagged at 3x
the 14-day average (5 s or more between requests).
Weekly (Monday 08:30 IST): Google autocomplete in English (gl=in, 2-4 s between requests, snapshots cached in .state/),
where a suggestion missing from the last snapshot is rising; Trends rising related queries for the seed topics.
Monthly (1st, 09:00 IST): Trends interest over 12 months for the tracked keywords, anchored on "animal welfare" = 100.

Google Trends and autocomplete are unofficial and often refuse GitHub's addresses: each gets one paced attempt per run.
A refusal stops that source for the run, keeps what it had, and is reported to the site, where the owner pastes Trends
figures into the form on the Rising tab instead. Nothing here reads Google search results.

The site's lists (keywords, topics, species, watchlists, seeds) come from GET aw/v1/search-insights; results go to POST
aw/v1/search-insights, one request per source, with the ingest user's application password (ingest.Client). The site
stores rising terms in wp_aw_si_rising and the figures in wp_aw_si_obs, and records each source's run for its notices.
Hindi and Hinglish wait for phase 5: only English (Latin-script) terms are kept.

Run: python -m aw_workers.search_insights daily|weekly|monthly|all [--days 7] [--top 10] [--config file.json]
DRY_RUN is on by default: it fetches and prints the rising terms, and sends nothing.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import quote

import requests

from . import counts, ingest, relay

log = logging.getLogger("aw_workers.search_insights")

TIMEOUT = 30
CONTACT = os.environ.get("AW_CONTACT", "https://animalwrites.in; admin@wugweb.com")
UA = f"AnimalWritesSearchInsights/1.0 ({CONTACT})"
STATE = Path(os.environ.get("AW_STATE_DIR", ".state")) / "search-insights"

JOBS = {
    "daily": ["trends_rss", "wikipedia", "gdelt"],
    "weekly": ["autocomplete", "trends_related"],
    "monthly": ["trends_interest"],
}

WIKI_RATIO = 2.0      # flag at 2x the 30-day average
WIKI_MIN_VIEWS = 50   # and at least this many views that day (a page read 3 times instead of 1 is no trend)
GDELT_RATIO = 3.0     # flag at 3x the 14-day average, as the Sourcing spikes on the site
GDELT_MIN = 5         # and at least 5 articles that day
WIKI_PAUSE = 1.0
GDELT_PAUSE = 5.5
RELAY_HOSTS = ("gdeltproject.org",)  # asked through the site when refused from GitHub (relay switch on in Sourcing → Sources)
AC_PAUSE = (2.0, 4.0)
TRENDS_PAUSE = (2.0, 4.0)
AC_CACHE_DAYS = 6     # a seed fetched within 6 days is not asked again (a re-run in the same week sends no request)
ANCHOR = "animal welfare"

# Used when the site does not send a list (dry runs without the site's password, or an older site).
DEFAULTS = {
    "keywords": [],
    "topics": ["stray dogs", "street dogs", "animal welfare", "animal cruelty", "rabies", "dog bite", "gaushala",
               "cow protection", "wildlife", "animal rescue", "animal shelter", "veterinary hospital", "abc rules",
               "pca act", "awbi", "human wildlife conflict", "elephant", "leopard", "tiger"],
    "species": ["dog", "dogs", "puppy", "puppies", "cat", "cats", "kitten", "cow", "cows", "cattle", "buffalo",
                "calf", "goat", "sheep", "pig", "horse", "donkey", "camel", "monkey", "macaque", "langur", "elephant",
                "tiger", "leopard", "lion", "cheetah", "bear", "deer", "blackbuck", "peacock", "peafowl", "pigeon",
                "parrot", "bird", "birds", "vulture", "eagle", "owl", "crow", "snake", "cobra",
                "crocodile", "turtle", "tortoise", "dolphin", "whale", "fish", "pangolin", "rhino",
                "rhinoceros", "wolf", "jackal", "hyena", "boar", "squirrel", "rabbit", "chicken", "hen",
                "poultry", "mule"],
    # Canonical titles (redirects count apart in the pageviews API): checked against en.wikipedia on 24 Oct 2026.
    "wikipedia": ["Rabies", "Free-ranging dog", "Indian pariah dog", "Animal Welfare Board of India",
                  "Prevention of Cruelty to Animals Act, 1960", "Wild Life (Protection) Act, 1972", "Goshala",
                  "Cattle slaughter in India", "Cow protection movement", "Cow vigilante violence in India",
                  "Animal welfare and rights in India", "Human–wildlife conflict", "Project Tiger", "Dog bite"],
    "gdelt": ["stray dogs", "dog bite", "rabies", "animal cruelty", "gaushala", "cow vigilante", "human wildlife conflict",
              "leopard attack", "elephant death", "animal welfare board"],
    "autocomplete": ["stray dogs", "street dogs", "dog bite", "rabies", "animal welfare", "animal cruelty",
                     "animal rescue", "animal shelter", "gaushala", "veterinary hospital", "pet adoption",
                     "abc rules", "pca act", "feeding stray dogs", "wildlife rescue"],
    "trends_related": ["stray dogs", "animal welfare", "rabies", "animal cruelty", "gaushala"],
    "interest": [],
    "anchor": ANCHOR,
    # Terms with these words are not about animals in India: an anime called Bungo Stray Dogs fills "stray dogs" related
    # searches, films and songs the trending list. The site sends its own list (filter aw_si_rising_lists).
    "exclude": ["anime", "manga", "bungo", "movie", "film", "song", "lyrics", "season", "episode", "cast", "trailer",
                "netflix", "web series", "ott", "box office", "game", "meme", "wallpaper", "cartoon", "meaning in",
                "cricket", "ipl", "vs", "odi", "t20", "match", "score", "shroff"],
}

GENERIC = {"animal", "animals", "welfare", "india", "indian", "save", "help", "news", "care", "update", "updates",
           "issue", "case", "story", "rules", "act", "law", "the", "and", "for", "of", "in", "near", "me"}

NOT_PLURAL = {"news", "rabies", "species", "series", "scabies", "diabetes", "herpes", "mumps", "measles", "always",
              "perhaps", "whereas", "christmas"}


KIND = {"species": "species", "topics": "topic", "keywords": "keyword"}


class Refused(Exception):
    """The service refused us (429, 403, a CAPTCHA page): one attempt per run, so the source stops here."""


def norm(s: str) -> str:
    """The site's aw_si_norm: lower case, dots and apostrophes dropped, other punctuation as spaces, English plurals
    folded on words of 5 letters or more (gaushalas -> gaushala, leopards -> leopard; "dogs" stays, as on the site), so a
    term matches its keyword record."""
    s = re.sub(r"<[^>]+>", "", s or "").lower()
    s = s.replace(".", "").replace("'", "").replace("’", "")
    s = re.sub(r"[^\w{}]+|_", " ", s, flags=re.UNICODE)
    words = s.split()
    for i, w in enumerate(words):
        if re.fullmatch(r"[a-z]{4,}s", w) and not re.search(r"(ss|us|is|ous)$", w) and w not in NOT_PLURAL:
            words[i] = w[:-1]
    return " ".join(words)[:191]


def english(term: str) -> bool:
    """Latin script only: Hindi and other scripts wait for phase 5 (owner, 24 Oct 2026)."""
    return bool(term.strip()) and all(ord(c) < 0x250 or c in "–—’‘“”" for c in term)


def today_ist() -> date:
    return (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).date()


# ------------------------------------------------------------------ fetching


class Fetcher:
    """GET with our user-agent, pacing between requests to the same service, and a refusal check."""

    def __init__(self, session=None, sleep: Callable[[float], None] = time.sleep, rand: Callable[[float, float], float] = random.uniform):
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = UA  # requests sets its own by default, which Wikimedia refuses
        self.sleep = sleep
        self.rand = rand
        self.last: dict[str, float] = {}
        self.refused: dict[str, str] = {}  # service -> why: one attempt per service per run, across sources
        self.requests = 0

    def pace(self, service: str, gap: float | tuple[float, float]) -> None:
        wait = self.rand(*gap) if isinstance(gap, tuple) else gap
        if service in self.last:
            left = wait - (time.monotonic() - self.last[service])
            if left > 0:
                self.sleep(left)

    def get(self, service: str, url: str, gap: float | tuple[float, float] = 0, **kw) -> requests.Response:
        if service in self.refused:
            raise Refused(self.refused[service] + " (earlier in this run; not asked again)")
        self.pace(service, gap)
        try:
            self.requests += 1
            resp = self.session.get(url, timeout=kw.pop("timeout", TIMEOUT), **kw)
        finally:
            self.last[service] = time.monotonic()
        if resp.status_code in (403, 429) or "/sorry/" in (resp.url or ""):
            # Refused from GitHub's servers (GDELT has answered 429 to the first request of every run since 25 Oct
            # 2026): a host whose relay switch is on (Sourcing → Sources) is asked once more through the site.
            via = relay.get(url) if relay.allowed(url) else None
            if via is not None and via.status_code == 200:
                log.info("%s: refused from GitHub (HTTP %s), read through the site", service, resp.status_code)
                return via
            self.refused[service] = f"{service} refused the request (HTTP {resp.status_code})" + (
                f"; through the site: HTTP {via.status_code}" if via is not None else "")
            raise Refused(self.refused[service])
        return resp


# ------------------------------------------------------------------ vocabulary


def vocabulary(config: dict) -> list[tuple[str, str]]:
    """(normalised phrase, kind) pairs a Trends term must name: our keywords, topics and species. Single words must be
    specific ("rabies", "leopard"), never generic ("india", "animal")."""
    out: dict[str, str] = {}
    for kind in ("species", "topics", "keywords"):
        for label in config.get(kind) or []:
            n = norm(str(label))
            if not n or "{" in n:
                continue
            words = n.split()
            if len(words) == 1 and (n in GENERIC or len(n) < 3):
                continue
            if all(w in GENERIC for w in words):
                continue
            out.setdefault(n, kind)
    return sorted(out.items(), key=lambda kv: -len(kv[0]))  # longest phrase first: the most specific match wins


def names(text: str, vocab: list[tuple[str, str]]) -> tuple[str, str]:
    """The first vocabulary phrase the text names as whole words: (phrase, kind), or ('', '')."""
    padded = f" {norm(text)} "
    for phrase, kind in vocab:
        if f" {phrase} " in padded:
            return phrase, kind
    return "", ""


# ------------------------------------------------------------------ sources


def result(source: str) -> dict:
    return {"source": source, "rising": [], "obs": [], "info": {}, "error": ""}


def trends_rss(f: Fetcher, config: dict, today: date, days: int = 1) -> dict:
    """Trending now in India (one request). A term is kept when it, or a news headline under it, names our vocabulary."""
    out = result("trends_rss")
    vocab = vocabulary(config)
    resp = f.get("trends", "https://trends.google.com/trending/rss?geo=IN")
    resp.raise_for_status()
    ns = {"ht": "https://trends.google.com/trending/rss"}
    root = ET.fromstring(resp.content)
    items = root.findall("./channel/item")
    oldest = today - timedelta(days=days)
    for it in items:
        term = (it.findtext("title") or "").strip()
        traffic = (it.findtext("ht:approx_traffic", default="", namespaces=ns) or "").strip()
        heads = [(n.findtext("ht:news_item_title", default="", namespaces=ns) or "").strip() for n in it.findall("ht:news_item", ns)]
        try:
            seen = datetime.strptime((it.findtext("pubDate") or "").strip(), "%a, %d %b %Y %H:%M:%S %z").astimezone(timezone(timedelta(hours=5, minutes=30))).date()
        except ValueError:
            seen = today
        if not english(term) or seen < oldest:
            continue
        phrase, kind = names(term, vocab)
        where = "term"
        if not phrase:
            # A headline needs a phrase or one of our topics or keywords: a single animal word in a headline is too
            # loose ("India opt to bat first", "Tiger Shroff").
            loose = [(p, k) for p, k in vocab if " " in p or k != "species"]
            for h in heads:
                phrase, kind = names(h, loose)
                if phrase:
                    where = "news"
                    break
        if not phrase:
            continue
        n = int(re.sub(r"\D", "", traffic) or 0)
        note = f"Trending in India, {traffic} searches; names the {KIND[kind]} “{phrase}”"
        if where == "news":
            note += " in its news"
        out["rising"].append({"term": term, "growth": None, "d": seen.isoformat(), "note": note[:250]})
        out["obs"].append({"term": term, "metric": "traffic", "value": n, "period": "day", "d": seen.isoformat(), "txt": traffic})
    out["info"] = {"trending": len(items), "kept": len(out["rising"])}
    return out


def _wiki_series(f: Fetcher, article: str, start: date, end: date) -> dict[date, int]:
    title = quote(article.replace(" ", "_"), safe="")
    url = (f"https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/en.wikipedia/all-access/user/{title}/daily/"
           f"{start:%Y%m%d}/{end:%Y%m%d}")
    resp = f.get("wikipedia", url, WIKI_PAUSE)
    if resp.status_code == 404:
        return {}
    resp.raise_for_status()
    out = {}
    for row in resp.json().get("items", []):
        out[datetime.strptime(row["timestamp"][:8], "%Y%m%d").date()] = int(row.get("views", 0))
    return out


def spikes(series: dict[date, int], day: date, window: int, ratio: float, minimum: int) -> float | None:
    """The day's value against the mean of the `window` days before it: the ratio when it is a spike, else None.
    Days missing from the series count as 0. A value with no history (mean 0) is a spike at the minimum, ratio inf."""
    value = series.get(day, 0)
    if value < minimum:
        return None
    before = [series.get(day - timedelta(days=i), 0) for i in range(1, window + 1)]
    mean = sum(before) / window
    if mean == 0:
        return float("inf")
    r = value / mean
    return round(r, 2) if r >= ratio else None


def wikipedia(f: Fetcher, config: dict, today: date, days: int = 1) -> dict:
    """Daily views of the watchlist's English Wikipedia pages; a day at 2x the 30 days before it is rising."""
    out = result("wikipedia")
    end = today - timedelta(days=1)  # yesterday: the API has whole days only
    start = end - timedelta(days=days - 1 + 30)
    missing, failed = [], []
    for article in config.get("wikipedia") or []:
        try:
            series = _wiki_series(f, article, start, end)
        except Refused as exc:  # stop here, keep the pages already read
            out["error"] = f"{exc} after {len(out['obs']) // max(days, 1)} pages"
            break
        except Exception as exc:  # one bad title or a server error skips that page only
            failed.append(f"{article}: {str(exc)[:80]}")
            continue
        if not series:
            missing.append(article)
            continue
        for i in range(days):
            d = end - timedelta(days=i)
            out["obs"].append({"term": article, "metric": "views", "value": series.get(d, 0), "period": "day", "d": d.isoformat(), "txt": ""})
            r = spikes(series, d, 30, WIKI_RATIO, WIKI_MIN_VIEWS)
            if r is not None:
                growth = None if r == float("inf") else r
                avg = round(sum(series.get(d - timedelta(days=k), 0) for k in range(1, 31)) / 30)
                out["rising"].append({"term": article, "growth": growth, "d": d.isoformat(),
                                      "note": f"Wikipedia: {series.get(d, 0):,} views on {d:%-d %b}, 30-day average {avg:,}"})
    out["info"] = {"pages": len(out["obs"]) // max(days, 1), "missing": missing, "failed": failed}
    if failed and not out["obs"] and not out["error"]:
        out["error"] = "Wikipedia answered no page today: " + "; ".join(failed)[:200]
    return out


def _gdelt_series(f: Fetcher, topic: str, days: int) -> dict[date, int]:
    q = quote(f'"{topic}" sourcecountry:IN')
    url = f"https://api.gdeltproject.org/api/v2/doc/doc?query={q}&mode=timelinevolraw&format=json&timespan={days + 15}d"
    resp = f.get("gdelt", url, GDELT_PAUSE, timeout=60)
    resp.raise_for_status()
    try:
        data = json.loads(resp.text)  # also a relayed answer, which has no .json()
    except ValueError as exc:  # GDELT answers a malformed query with plain text
        raise ValueError(f"GDELT: {resp.text[:120]}") from exc
    out: dict[date, int] = {}
    for series in data.get("timeline") or []:
        if "count" not in str(series.get("series", "")).lower() and len(data.get("timeline")) > 1:
            continue
        for pt in series.get("data") or []:
            d = datetime.strptime(str(pt["date"])[:8], "%Y%m%d").date()
            out[d] = out.get(d, 0) + int(pt.get("value", 0))
    return out


def gdelt(f: Fetcher, config: dict, today: date, days: int = 1) -> dict:
    """Articles a day from Indian sources naming each watchlist topic; a day at 3x the 14 days before it is rising.
    A failed topic is skipped for the day; the site keeps the last data."""
    out = result("gdelt")
    failed = []
    done = 0
    end = today - timedelta(days=1)
    for topic in config.get("gdelt") or []:
        try:
            series = _gdelt_series(f, topic, days)
        except Refused as exc:  # stop here, keep the topics already read
            out["error"] = f"{exc} after {done} topics"
            break
        except Exception as exc:
            failed.append(f"{topic}: {str(exc)[:80]}")
            continue
        for i in range(days):
            d = end - timedelta(days=i)
            out["obs"].append({"term": topic, "metric": "articles", "value": series.get(d, 0), "period": "day", "d": d.isoformat(), "txt": ""})
            r = spikes(series, d, 14, GDELT_RATIO, GDELT_MIN)
            if r is not None:
                avg = sum(series.get(d - timedelta(days=k), 0) for k in range(1, 15)) / 14
                out["rising"].append({"term": topic, "growth": None if r == float("inf") else r, "d": d.isoformat(),
                                      "note": f"News (GDELT, Indian sources): {series.get(d, 0)} articles on {d:%-d %b}, 14-day average {avg:.1f}"})
        done += 1
    out["info"] = {"topics": done, "failed": failed}
    if failed and not done and not out["error"]:
        out["error"] = "GDELT answered no topic today: " + "; ".join(failed)[:200]
    return out


def _state(name: str) -> dict:
    p = STATE / name
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except ValueError:
        return {}


def _save_state(name: str, data: dict) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / name).write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True))


def autocomplete(f: Fetcher, config: dict, today: date, days: int = 7, save: bool | None = None) -> dict:
    """English Google suggestions (gl=in) for each seed. A suggestion missing from the seed's last snapshot is rising;
    a seed seen for the first time only stores its snapshot. A refusal stops the remaining seeds (one attempt)."""
    out = result("autocomplete")
    if save is None:
        save = not config.get("_dry_run")  # a dry run never moves the snapshots the next live run compares against
    state = _state("autocomplete.json")
    asked = cached = baseline = 0
    try:
        for seed in config.get("autocomplete") or []:
            old = state.get(seed) or {}
            if old.get("date") and (today - date.fromisoformat(old["date"])).days < AC_CACHE_DAYS and "prev" in old:
                cached += 1
                current, prev = old["suggestions"], old["prev"]
            else:
                url = f"https://suggestqueries.google.com/complete/search?client=firefox&hl=en&gl=in&q={quote(seed)}"
                resp = f.get("autocomplete", url, AC_PAUSE)
                resp.raise_for_status()
                try:
                    current = [s for s in resp.json()[1] if isinstance(s, str) and english(s)]
                except (ValueError, IndexError, TypeError) as exc:
                    raise Refused(f"autocomplete answered something that is not a suggestion list ({str(exc)[:60]})") from exc
                asked += 1
                prev = old.get("suggestions")
                state[seed] = {"date": today.isoformat(), "suggestions": current, "prev": prev}
            if prev is None:
                baseline += 1
                continue
            for s in current:
                if s not in prev and norm(s) != norm(seed):
                    out["rising"].append({"term": s, "growth": None, "d": today.isoformat(), "note": f"New Google suggestion for “{seed}” this week"})
    except Refused as exc:
        out["error"] = f"{exc} after {asked} seeds. The bookmarklet runs the same seeds (phase 4)."
    except requests.RequestException as exc:
        out["error"] = f"autocomplete failed after {asked} seeds: {str(exc)[:120]}"
    finally:
        if save:
            _save_state("autocomplete.json", state)
    out["info"] = {"asked": asked, "cached": cached, "baseline": baseline}
    return out


class Trends:
    """Google Trends' own web endpoints (unofficial): explore gives widget tokens, widgetdata the figures."""

    BASE = "https://trends.google.com/trends/api"

    def __init__(self, f: Fetcher):
        self.f = f
        self.primed = False

    def _json(self, resp) -> dict:
        resp.raise_for_status()
        text = resp.text
        return json.loads(text[text.find("{"):])  # the body starts with )]}'

    def _get(self, path: str, params: dict) -> dict:
        if not self.primed:  # Trends wants its cookie first
            self.f.get("trends", "https://trends.google.com/trends/explore?geo=IN&hl=en-IN", TRENDS_PAUSE)
            self.primed = True
        return self._json(self.f.get("trends", f"{self.BASE}/{path}", TRENDS_PAUSE, params=params))

    def widgets(self, keywords: list[str], period: str) -> dict:
        req = {"comparisonItem": [{"keyword": k, "geo": "IN", "time": period} for k in keywords], "category": 0, "property": ""}
        data = self._get("explore", {"hl": "en-IN", "tz": "-330", "req": json.dumps(req)})
        return {w.get("id", ""): w for w in data.get("widgets", [])}

    def widget_data(self, kind: str, widget: dict) -> dict:
        return self._get(f"widgetdata/{kind}", {"hl": "en-IN", "tz": "-330", "req": json.dumps(widget["request"]), "token": widget["token"]})


def growth_of(formatted: str, value) -> tuple[float | None, str]:
    """Trends' rising value as a ratio to before (+250% -> 3.5); Breakout (over +5,000%) has no number."""
    if "breakout" in str(formatted).lower():
        return None, "Breakout (more than +5,000%)"
    try:
        pct = float(str(value).replace(",", ""))
    except ValueError:
        return None, str(formatted)
    return round(1 + pct / 100, 2), f"+{pct:,.0f}%"


def trends_related(f: Fetcher, config: dict, today: date, days: int = 7) -> dict:
    """Rising related searches in India over the last month for each seed topic. One attempt: a refusal stops it."""
    out = result("trends_related")
    t = Trends(f)
    done = 0
    try:
        for seed in config.get("trends_related") or []:
            w = t.widgets([seed], "today 1-m")
            rel = next((v for k, v in w.items() if k.startswith("RELATED_QUERIES")), None)
            if not rel:
                continue
            data = t.widget_data("relatedsearches", rel)
            lists = data.get("default", {}).get("rankedList", [])
            rising = lists[1].get("rankedKeyword", []) if len(lists) > 1 else []
            for r in rising:
                q = str(r.get("query", "")).strip()
                if not english(q):
                    continue
                g, label = growth_of(r.get("formattedValue", ""), r.get("value", ""))
                out["rising"].append({"term": q, "growth": g, "d": today.isoformat(), "note": f"Trends: rising search related to “{seed}”, {label}"})
            done += 1
    except Refused as exc:
        out["error"] = f"{exc} after {done} seed topics. Paste the rising queries on the Rising tab instead."
    except (ValueError, KeyError, requests.RequestException) as exc:
        out["error"] = f"Trends answered in an unexpected form ({str(exc)[:80]}) after {done} seed topics. Paste the rising queries on the Rising tab instead."
    out["info"] = {"seeds": done}
    return out


def trends_interest(f: Fetcher, config: dict, today: date, days: int = 30) -> dict:
    """Average interest over 12 months in India, four keywords at a time next to the anchor, scaled so the anchor
    ("animal welfare") is 100. Stored as Trends T for the keyword's priority. One attempt: a refusal stops it."""
    out = result("trends_interest")
    anchor = config.get("anchor") or ANCHOR
    words = [k for k in dict.fromkeys(config.get("interest") or []) if english(k) and norm(k) != norm(anchor)]
    t = Trends(f)
    month = today.replace(day=1).isoformat()
    done = 0
    try:
        for i in range(0, len(words), 4):
            batch = words[i:i + 4]
            w = t.widgets([anchor] + batch, "today 12-m")
            ts = w.get("TIMESERIES")
            if not ts:
                continue
            avgs = t.widget_data("multiline", ts).get("default", {}).get("averages") or []
            if len(avgs) != len(batch) + 1 or not avgs[0]:
                continue
            for kw, a in zip(batch, avgs[1:]):
                out["obs"].append({"term": kw, "metric": "index", "value": round(a * 100 / avgs[0]), "period": "12m", "d": month,
                                   "txt": f"{anchor} = 100"})
            done += len(batch)
    except Refused as exc:
        out["error"] = f"{exc} after {done} of {len(words)} keywords. Paste the Trends averages on the Rising tab instead."
    except (ValueError, KeyError, requests.RequestException) as exc:
        out["error"] = f"Trends answered in an unexpected form ({str(exc)[:80]}) after {done} keywords. Paste the averages on the Rising tab instead."
    out["info"] = {"keywords": done, "asked": len(words)}
    return out


SOURCES: dict[str, Callable[..., dict]] = {
    "trends_rss": trends_rss,
    "wikipedia": wikipedia,
    "gdelt": gdelt,
    "autocomplete": autocomplete,
    "trends_related": trends_related,
    "trends_interest": trends_interest,
}


# ------------------------------------------------------------------ run


def load_config(client, path: str = "") -> dict:
    """The site's lists, else a file (dry runs), else the defaults above. Missing lists fall back to the defaults."""
    raw: dict = {}
    if path:
        raw = json.loads(Path(path).read_text())
    elif client is not None:
        raw = client.search_insights()
    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in (raw or {}).items() if v})
    return cfg


def excluded(term: str, words: list[str]) -> bool:
    """Whether a term has one of the exclude words or phrases (whole words, plurals folded as on the site)."""
    padded = f" {norm(term)} "
    return any(f" {norm(w)} " in padded or f" {norm(w)}s " in padded for w in words if norm(w))


def run_source(name: str, f: Fetcher, config: dict, today: date, days: int) -> dict:
    try:
        out = SOURCES[name](f, config, today, days)
        words = config.get("exclude") or []
        kept = [r for r in out["rising"] if not excluded(r["term"], words)]
        out["info"]["excluded"] = len(out["rising"]) - len(kept)
        out["rising"] = kept
        return out
    except Refused as exc:
        out = result(name)
        out["error"] = str(exc)
        return out
    except Exception as exc:  # one source failing never stops the others
        log.exception("%s failed", name)
        out = result(name)
        out["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        return out


def top(results: list[dict], n: int) -> list[dict]:
    """The rising terms of every source, one row per term (sources joined), highest growth first, then the most sources."""
    by: dict[str, dict] = {}
    for r in results:
        for row in r["rising"]:
            k = norm(row["term"])
            e = by.setdefault(k, {"term": row["term"], "sources": [], "growth": None, "first": row["d"], "note": row["note"]})
            if r["source"] not in e["sources"]:
                e["sources"].append(r["source"])
            if row["growth"] is not None and (e["growth"] is None or row["growth"] > e["growth"]):
                e["growth"] = row["growth"]
            e["first"] = min(e["first"], row["d"])
    return sorted(by.values(), key=lambda e: (-(e["growth"] or 0), -len(e["sources"]), e["term"]))[:n]


def table(rows: list[dict]) -> str:
    lines = ["| # | Term | Sources | Growth | First seen | Why |", "|---|---|---|---|---|---|"]
    for i, e in enumerate(rows, 1):
        g = f"×{e['growth']:.1f}" if e["growth"] else "–"
        lines.append(f"| {i} | {e['term']} | {', '.join(e['sources'])} | {g} | {e['first']} | {e['note'].replace('|', '/')} |")
    return "\n".join(lines)


def _days(value: str) -> int:
    """--days: 0 (each source's default) or 1 to 60."""
    try:
        n = int(str(value).strip())
    except ValueError:
        raise argparse.ArgumentTypeError(f"days must be a whole number, not {value!r}") from None
    if not 0 <= n <= 60:
        raise argparse.ArgumentTypeError("days must be between 1 and 60")
    return n


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("job", nargs="?", default=os.environ.get("AW_SI_JOB") or "daily", choices=[*JOBS, "all"])
    ap.add_argument("--days", type=_days, default=os.environ.get("AW_SI_DAYS") or "0", help="days to look back, 1-60 (default: 1 daily, 7 weekly)")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--config", default=os.environ.get("AW_SI_CONFIG", ""), help="a JSON file of the site's lists (dry runs)")
    ap.add_argument("--only", default=os.environ.get("AW_SI_ONLY", ""), help="comma-separated sources")
    a = ap.parse_args(argv)
    dry_run = str(os.environ.get("DRY_RUN", "true")).strip().lower() not in ("false", "0", "no", "off")
    client = None
    if os.environ.get("WP_BASE_URL") and os.environ.get("WP_INGEST_APP_PASSWORD"):
        client = ingest.Client(os.environ["WP_BASE_URL"], os.environ["WP_INGEST_USER"], os.environ["WP_INGEST_APP_PASSWORD"])
    elif not dry_run:
        log.error("WP_BASE_URL, WP_INGEST_USER and WP_INGEST_APP_PASSWORD are needed to send results")
        return 1
    try:
        config = load_config(client, a.config)
    except ingest.AuthError:
        raise
    except Exception as exc:
        if not dry_run:
            log.error("the site's lists are unavailable: %s", exc)
            return 1
        log.warning("the site's lists are unavailable (%s): using the defaults", exc)
        config = dict(DEFAULTS)
    config["_dry_run"] = dry_run
    if client is not None:
        relay.configure(client, RELAY_HOSTS)  # the site fetches only hosts whose relay switch is on there
    names_ = [s for j in (JOBS if a.job == "all" else [a.job]) for s in JOBS[j]]
    if a.only:
        names_ = [s for s in names_ if s in a.only.split(",")]
    today = today_ist()
    f = Fetcher()
    results, failed_posts = [], 0
    for name in names_:
        days = a.days or (7 if name in JOBS["weekly"] else 30 if name in JOBS["monthly"] else 1)
        r = run_source(name, f, config, today, days)
        r["days"] = days
        r["dry_run"] = dry_run
        results.append(r)
        log.info("%s: %d rising, %d figures%s", name, len(r["rising"]), len(r["obs"]), f"; error: {r['error']}" if r["error"] else "")
        if not dry_run:
            try:
                answer = client.search_insights_post(r)
                log.info("%s: site stored %s", name, answer)
            except ingest.AuthError:
                raise
            except Exception as exc:
                failed_posts += 1
                log.error("%s: the site did not take the results: %s", name, exc)
    best = top(results, a.top)
    print(table(best))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(f"## Search Insights ({a.job}{', dry run: nothing sent' if dry_run else ''})\n\n")
            fh.write("| Source | Rising | Figures | Error |\n|---|---|---|---|\n")
            for r in results:
                fh.write(f"| {r['source']} | {len(r['rising'])} | {len(r['obs'])} | {r['error'].replace('|', '/') or '–'} |\n")
            fh.write(f"\n### Top {a.top} rising\n\n{table(best)}\n")
    counts.write(rising=sum(len(r["rising"]) for r in results), figures=sum(len(r["obs"]) for r in results),
                 source_errors=sum(1 for r in results if r["error"]), requests=f.requests)
    # A source that answered with an error and no figures at all read nothing today: the run fails, so the Workers tab
    # shows it (GDELT was refused for 7 days in Oct 2026 while every run showed success).
    dead = [r["source"] for r in results if r["error"] and not r["obs"] and not r["rising"]]
    if dead:
        log.error("no data today from: %s", ", ".join(dead))
    return 1 if failed_posts or dead else 0


if __name__ == "__main__":
    raise SystemExit(main())
