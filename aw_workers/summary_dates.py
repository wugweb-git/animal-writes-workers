"""Backfill (owner, 15 Oct 2026): take publication dates given as event dates out of existing summaries.

  python -m aw_workers.summary_dates ITEMS.json PLAN.json [CACHE_DIR]

ITEMS.json is the site's export (id, type, excerpt, source_date, post_date, source_url, title), made on the server with
aw-wordpress/tools/summary-dates/export.php. For each summary that gives a date equal to the report's publication date
(aw_source_date, the post date, or the date the source page says it was published), the source page is read (politely:
one request at a time per host, a pause between them, timeouts, a cache on disk) and:
- the source says the event happened on that date → the summary is left as it is;
- it does not → the date phrase is taken out of the sentence (every other word stays). A sentence that only gave the
  date ("The incident occurred on 25 April 2026.") goes as a whole;
- the source can't be read, or the date can't be taken out cleanly → left, with the reason.
PLAN.json is applied on the server with aw-wordpress/tools/summary-dates/apply.php (previous text kept in
aw_summary_history; post_date and post_modified never change).
"""

from __future__ import annotations

import hashlib
import html as htmllib
import json
import logging
import re
import sys
import time
import urllib.parse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import event_dates as ed

log = logging.getLogger("aw_workers.summary_dates")

PAUSE_S = 2.0  # between two requests to the same host
TIMEOUT = 20

# A sentence left with nothing but "it happened" once the date is out: the whole sentence goes.
# --- reading the source -------------------------------------------------------------------------------------------

_PUBLISHED = [re.compile(p, re.I) for p in (
    r'<meta[^>]+(?:property|name|itemprop)=["\'](?:article:published_time|datePublished|pubdate|publish-date|'
    r'article:modified_time|og:updated_time|dateModified)["\'][^>]+content=["\']([^"\']+)',
    r'"(?:datePublished|dateModified)"\s*:\s*"([^"]+)"',
)]


BROWSER = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/129.0 Safari/537.36", "Accept": "text/html,application/xhtml+xml", "Accept-Language": "en-IN,en;q=0.9"}


def _article_body(raw: str) -> str:
    """The article text sites put in their JSON-LD (articleBody), for pages whose paragraphs are not <p> tags."""
    parts = []
    for m in re.finditer(r'"articleBody"\s*:\s*"((?:[^"\\]|\\.)*)"', raw):
        try:
            parts.append(json.loads('"' + m.group(1) + '"'))
        except ValueError:
            continue
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", " ".join(parts))).strip()


def read_page(url: str, session=None) -> dict:
    """{text, published: [dates], status} of a source page (our own User-Agent first, a browser's when refused)."""
    import requests

    from .enrich import UA
    from .merge import page_text

    if not url or url.lower().split("?")[0].endswith(".pdf"):
        return {"text": "", "published": [], "status": "pdf"}
    raw, status = "", "not fetched"
    for i, headers in enumerate((UA, BROWSER)):
        if i:
            time.sleep(PAUSE_S)
        try:
            resp = (session or requests).get(url, headers=headers, timeout=TIMEOUT)
            status = resp.status_code
            raw = resp.text[:1_500_000] if status < 400 else ""
        except Exception as exc:  # noqa: BLE001
            raw, status = "", type(exc).__name__
        if raw and (len(re.findall(r"(?i)<p\b", raw)) >= 3 or '"articleBody"' in raw):
            break
    published = sorted({str(ed.to_date(m.group(1))) for rx in _PUBLISHED for m in rx.finditer(raw) if ed.to_date(m.group(1))})

    class _Resp:  # page_text reads through a session; hand it the page already fetched
        text = raw

        def raise_for_status(self):
            return None

    class _S:
        def get(self, *a, **k):
            return _Resp()

    text = page_text(url, session=_S(), limit=20000) if raw else ""
    body = _article_body(raw)
    if body and body[:200] not in text:
        text = f"{text} {body}".strip()
    if len(text) < 200 and raw:  # paragraphs in <div>s (PIB: inside an ASP.NET <form>): the page's visible text
        page = re.sub(r"(?is)<(script|style|noscript|nav|header|footer|aside|select)\b.*?</\1>", " ", raw)
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page)).strip()[:20000]
    return {"text": fix_mojibake(htmllib.unescape(text)), "published": published, "status": status}


_MOJIBAKE = re.compile(r"[\xc2-\xf4][\x80-\xbf]{1,3}")


def fix_mojibake(text: str) -> str:
    """UTF-8 read as Latin-1 (25 Oct 2026: a page without a charset gave "Corporationâ\x80\x99s" and "Adopt Meâ\x80\x94",
    so "Corporation’s" and "Me" in the summary looked unsourced): each such run decoded again; anything else kept."""
    def fix(m):
        try:
            return m.group(0).encode("latin-1").decode("utf-8")
        except UnicodeError:
            return m.group(0)

    return _MOJIBAKE.sub(fix, text or "")


def fetch_all(urls: list[str], cache_dir: Path, session=None) -> dict[str, dict]:
    """Each URL once, from the cache when there; one host at a time per thread, PAUSE_S between its requests."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    out, by_host = {}, defaultdict(list)
    for u in dict.fromkeys(urls):
        f = cache_dir / (hashlib.sha1(u.encode()).hexdigest() + ".json")
        if f.exists():
            out[u] = json.loads(f.read_text())
        else:
            by_host[urllib.parse.urlparse(u).netloc].append(u)

    def run(host_urls):
        for i, u in enumerate(host_urls):
            if i:
                time.sleep(PAUSE_S)
            got = read_page(u, session)
            (cache_dir / (hashlib.sha1(u.encode()).hexdigest() + ".json")).write_text(json.dumps(got))
            out[u] = got

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(run, by_host.values()))
    return out


# --- deciding -----------------------------------------------------------------------------------------------------

def decide(item: dict, page: dict | None) -> dict:
    summary = item.get("excerpt") or ""
    pubs = [d for d in {item.get("source_date"), (item.get("post_date") or "")[:10], *((page or {}).get("published") or [])} if d]
    pub_dates = [ed.to_date(p) for p in pubs if ed.to_date(p)]
    hits = [m for m in ed.find_dates(summary) if any(m.same_day(p) for p in pub_dates)]
    base = {"id": item["id"], "type": item.get("type"), "before": summary, "dates": [m.text for m in hits]}
    if not hits:
        return dict(base, action="none")
    text = (page or {}).get("text") or ""
    if item.get("type") == "aw_legal" and (page or {}).get("status") == "pdf":
        return dict(base, action="kept", reason="Library document: the date is the document's own date")
    if len(text) < 200:
        why = "source is a PDF document" if (page or {}).get("status") == "pdf" else \
              f"source page not readable ({(page or {}).get('status', 'not fetched')})"
        return dict(base, action="left", reason=why)
    source = f"{item.get('title') or ''}. {text}"
    wrong = ed.pub_date_mentions(summary, pub_dates, source)
    if not wrong:
        return dict(base, action="kept", reason="the source gives this date as the event date")
    after = ed.remove_dates(summary, wrong)
    if not after:
        return dict(base, action="left", reason="the date can't be taken out of the sentence cleanly")
    if not ed.clean_removal(summary, after):
        return dict(base, action="left", reason="the date can't be taken out of the sentence cleanly")
    if len(re.findall(r"[\w'’-]+", after)) < 12:
        return dict(base, action="left", reason="too little left without the date")
    return dict(base, action="fix", after=after, removed=[m.text for m in wrong])


def main(argv=sys.argv[1:]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    items = json.loads(Path(argv[0]).read_text())
    cache = Path(argv[2]) if len(argv) > 2 else Path(argv[1]).parent / "page-cache"
    dated = [x for x in items if ed.find_dates(x.get("excerpt") or "")]
    pages = fetch_all([x["source_url"] for x in dated if x.get("source_url")], cache)
    plan = [decide(x, pages.get(x.get("source_url"))) for x in dated]
    Path(argv[1]).write_text(json.dumps(plan, ensure_ascii=False, indent=1))
    counts = Counter(p["action"] for p in plan)
    reasons = Counter(p.get("reason") for p in plan if p["action"] in ("left", "kept"))
    print(f"items: {len(items)}; summaries with a day date: {len(dated)}; {dict(counts)}")
    for r, n in reasons.most_common():
        print(f"  {n}: {r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
