"""POST items to the WordPress ingest endpoint."""

from __future__ import annotations

import os

import logging
import re
import time

import requests

from .titles import no_hyphens, no_section
from .sources import Item, allowed_categories, allowed_tags, categories_for, topic_tags_for

log = logging.getLogger(__name__)
TIMEOUT = 30
AUTH_RETRY_WAIT = 60  # seconds before the one retry of a request the site refused with 401/403


class AuthError(Exception):
    """Credentials rejected. Stops the whole run."""


class IngestError(Exception):
    pass


_COURTS = [
    ("supreme-court", r"supreme court|\bsc\b"),
    ("high-court", r"high court|\bhc\b"),
    ("national-green-tribunal", r"green tribunal|\bngt\b"),
]


def court_type(title: str) -> str:
    """Which court decided, from a headline. Legal-news headlines name the deciding court after
    the last colon ("...: Delhi High Court"), so that part wins; then full names over abbreviations."""
    t = title.lower()
    tail = t.rsplit(":", 1)[1] if ":" in t else ""
    for part in (tail, t):
        if not part:
            continue
        for full_only in (True, False):
            hits = []
            for slug, pat in _COURTS:
                pat = pat.split("|")[0] if full_only else pat
                m = re.search(pat, part)
                if m:
                    hits.append((m.start(), slug))
            if hits:
                return min(hits)[1]
    return ""


def build_payload(item: Item, summary: str, categories: list[str] | None = None) -> dict:
    payload = {
        "type": item.source.type,
        "title": no_hyphens(no_section(item.title, item.publisher, item.source.name, item.url.split("/")[2] if "//" in item.url else "")),  # no publisher or section suffix ("| India News", "- Blog"), no hyphens joining words (owner, 18 and 20 Oct 2026)
        "summary": summary,
        "source_url": item.url,
        "source_name": item.publisher or item.source.name,
        "source_date": item.date.isoformat() if item.date else "",
        "categories": (categories if categories is not None else categories_for(item)) + (["courts-judgments"] if item.source.source_type == "auto-court" else []),
        "feed": item.source.id,
    }
    allowed = allowed_categories(item.source.type)  # only the section's categories (Settings → Sourcing → Sections)
    payload["categories"] = [c for c in dict.fromkeys(payload["categories"]) if c in allowed]
    tags: list[str] = []
    source_type = court_type(item.title) if item.source.source_type == "auto-court" else item.source.source_type
    if source_type:
        payload["source_type"] = source_type
    if item.image:
        payload["image_url"] = item.image
    if item.doc_kind:  # Library documents: what it is (a tag), the court, and the PDF the site may keep a copy of
        tags.append(item.doc_kind)
        payload["document_url"] = item.document_url
        if item.legal_number:  # the site refuses a notification it already holds under the same number
            payload["legal_number"] = item.legal_number
        if item.draft:
            payload["draft"] = True
        if item.court:
            payload["court"] = item.court
            payload.setdefault("source_type", item.court)
    if item.source.type == "opportunity":
        payload["org_name"] = item.source.org or item.source.name
        payload["place"] = item.place
        payload["deadline"] = item.deadline.isoformat() if item.deadline else ""
        if item.employment_type:
            payload["employment_type"] = item.employment_type  # only when the posting states it
        if item.source.opportunity_type:
            tags.append(item.source.opportunity_type)
    topic, hold = topic_tags_for(f"{item.title} {item.text} {summary}")
    tags += topic
    if hold:
        payload["hold"] = hold  # the site keeps it in To review whatever the other checks say
    known = allowed_tags(item.source.type)
    # campaign-* slugs are not tags: the site files the item under that campaign (its Campaign link, 20 Oct 2026)
    tags = [t for t in dict.fromkeys(tags) if t and (known is None or t in known or t.startswith("campaign-"))]
    if tags:
        payload["tags"] = tags  # WordPress tags (Library type, Opportunity type ...) and campaign slugs
    if item.source.file_as == "press":
        return press_payload(payload)
    return payload


def press_payload(payload: dict) -> dict:
    """Coverage of a campaign topic goes to Media & PR (owner, 17 Oct 2026): the site's press ingest keeps it as a press
    item to review, with its campaign-* tags, so it shows under Media & PR and on the campaign page once approved."""
    from . import press

    platform = press.platform_of(payload["source_url"])
    out = {
        "type": "press", "title": payload["title"][:200], "source_url": payload["source_url"],
        "source_name": payload["source_name"], "source_date": payload["source_date"], "platform": platform,
        "format": press.format_of(payload["source_url"], platform), "account_handle": "",
        "image_url": payload.get("image_url", ""), "query": payload["feed"], "feed": payload["feed"],
        "tags": [t for t in payload.get("tags", []) if t.startswith("campaign-")],
    }
    return out


def opportunity_summary(item: Item) -> str:
    """Opportunities get a factual line built from the listing itself (no AI wording, nothing invented)."""
    org = item.source.org or item.source.name
    parts = [f"{org}: {item.title}."]
    if item.place:
        parts.append(f"Location: {item.place}.")
    if item.deadline:
        parts.append(f"Last date: {item.deadline.strftime('%-d %b %Y')}.")
    parts.append("Details and how to apply are on the organisation's page.")
    return " ".join(parts)


class Client:
    def __init__(self, base_url: str, user: str, app_password: str, session=None):
        self.url = base_url.rstrip("/") + "/wp-json/aw/v1/ingest"
        self.session = session or requests.Session()
        self.session.auth = (user, app_password)
        self.last: dict = {}
        # title -> (title, flag): press titles in sentence case on the way in, set by a run that has the provider chain
        # (headline_case.press_caser; owner, 20 Oct 2026)
        self.title_caser = None
        self.sleep = time.sleep
        self.refused = False  # the one retry already failed in this run

    def _send(self, send, *args, **kw):
        """send(*args, **kw) once; a 401/403 is tried once more after AUTH_RETRY_WAIT seconds. The site refused the
        workers' own login for one run at a time (25 and 30 Oct 2026), and the next run worked with the same password."""
        resp = send(*args, **kw)
        if resp.status_code in (401, 403) and not self.refused:
            log.warning("site answered HTTP %s, trying once more in %ss", resp.status_code, AUTH_RETRY_WAIT)
            self.sleep(AUTH_RETRY_WAIT)
            resp = send(*args, **kw)
            self.refused = resp.status_code in (401, 403)  # refused twice: later refusals in this run fail at once
        return resp

    def policy(self) -> dict:
        """Sourcing policy from WP Admin (News → Sourcing)."""
        resp = self._send(self.session.get, self.url.replace("/ingest", "/sourcing-policy"), timeout=TIMEOUT)
        if resp.status_code in (401, 403):
            raise AuthError(f"policy rejected credentials: HTTP {resp.status_code}")
        resp.raise_for_status()
        return resp.json()

    def _call(self, method: str, path: str, **kw):
        resp = self._send(self.session.request, method, self.url.replace("/ingest", path), timeout=kw.pop("timeout", TIMEOUT), **kw)
        if resp.status_code in (401, 403):
            raise AuthError(f"{path} rejected credentials: HTTP {resp.status_code}")
        resp.raise_for_status()
        return resp.json()

    def relevance_config(self) -> dict:
        """Categories (slug => name), each section's allowed categories and tags, and Directory organisation names, for the relevance gate."""
        return self._call("GET", "/relevance-config", timeout=90)

    def summary_rules(self) -> dict:
        """The summary rules (Settings → Sourcing → Summary rules): lengths per section, structure, wording."""
        return self._call("GET", "/summary-rules")

    def reject(self, entry: dict) -> dict:
        """Add an item the relevance gate refused to the site's Rejected log (Inbox → To review)."""
        return self._call("POST", "/relevance-rejected", json=entry)

    def reject_many(self, entries: list[dict]) -> dict:
        """Several rejections in one request (the site takes {"entries": [...]})."""
        return self._call("POST", "/relevance-rejected", json={"entries": entries})

    def exists(self, dois: list[str], urls: list[str], index: bool = False) -> dict:
        """Which of these research papers the site already has (aw/v1/exists, owner 24 Oct 2026): {dois: [...], urls: [...]};
        with index=True also "studies": [{id, title, first_author, year}] for the same-study check."""
        return self._call("POST", "/exists", json={"dois": dois, "urls": urls, "index": index}, timeout=60)

    def story(self, story_id: int) -> dict:
        """A story already on the site, for the same-event merge: title, summary, credits and added facts."""
        return self._call("GET", "/same-story", params={"id": int(story_id)})

    def same_story(self, body: dict) -> dict:
        """Fold a same-event report into the first story (credit, and the facts it adds); the date never changes."""
        return self._call("POST", "/same-story", json=body)

    def relevance_alert(self, held: int) -> dict:
        """Tell the site how many items the relevance check had to hold back this run (0 = it worked: clears the notice).
        The site emails the owner and shows a dashboard notice (owner, 12 Oct 2026)."""
        run = os.environ.get("GITHUB_RUN_ID", "")
        link = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/{run}" if run else ""
        return self._call("POST", "/relevance-alert", json={"held": int(held), "run": run, "link": link})

    def ingest_heartbeat(self, new: int, *, published: int | None = None, sources: int = 0, sources_failed: int = 0,
                         errors: list[str] | None = None, lists: str = "") -> dict:
        """Report this run: items created and published, sources tried and failed. The site emails the owner when half
        the sources fail; its own hourly check emails when nothing has been published for 12 hours or no run has
        reported for 8 hours (owner, 12 Oct 2026)."""
        run = os.environ.get("GITHUB_RUN_ID", "")
        link = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/{run}" if run else ""
        body = {"new": int(new), "link": link, "sources": int(sources), "sources_failed": int(sources_failed),
                "errors": [str(e)[:200] for e in (errors or [])][:10]}
        if published is not None:
            body["published"] = int(published)
        if lists:
            body["lists"] = lists  # site or yaml: which lists the run used
        return self._call("POST", "/ingest-heartbeat", json=body)

    def relevance_items(self, page: int, per_page: int = 100, missing_only: bool = False) -> dict:
        """Existing News, Library, Resources, Opportunities and Press items (any status but trash), one page."""
        return self._call("GET", "/relevance-items", params={"page": page, "per_page": per_page, "missing": int(missing_only)}, timeout=90)

    def relevance_verdicts(self, verdicts: list[dict]) -> dict:
        """Store verdicts on existing items (meta aw_relevance). Never changes an item's status."""
        return self._call("POST", "/relevance-verdicts", json={"verdicts": verdicts}, timeout=90)

    def seo_items(self, post_type: str, page: int, per_page: int = 100, missing_only: bool = True) -> dict:
        """Published items of a type for the focus-keyword job: id, title, excerpt, has_focus (aw-seo-extras)."""
        return self._call("GET", "/seo-items", params={"type": post_type, "page": page, "per_page": per_page,
                                                         "missing": int(missing_only)}, timeout=90)

    def seo_focus(self, items: list[dict], replace: bool = False) -> dict:
        """Write focus keywords [{id, keyword}]; the site writes where Rank Math's field is still empty, and with replace
        also over a primary keyword that is not in the item's SEO title."""
        body = {"items": items, "replace": True} if replace else {"items": items}
        return self._call("POST", "/seo-focus", json=body, timeout=90)

    def search_insights(self) -> dict:
        """Search Insights' lists for the rising-terms job: keywords, topics, species, watchlists and seeds."""
        return self._call("GET", "/search-insights", timeout=60)

    def search_insights_post(self, body: dict) -> dict:
        """One source's rising terms and figures (aw/v1/search-insights); the site records the source's run."""
        return self._call("POST", "/search-insights", json=body, timeout=90)

    def post(self, payload: dict) -> str:
        """Returns 'created', 'duplicate', or 'blocked' / 'filtered' (refused by the site's sourcing policy)."""
        if self.title_caser and payload.get("type") == "press" and payload.get("title"):
            payload["title"], flag = self.title_caser(payload["title"])
            if flag:
                payload["title_flag"] = "; ".join(filter(None, [payload.get("title_flag", ""), flag]))  # the site notes why
        resp = self._send(self.session.post, self.url, json=payload, timeout=TIMEOUT)
        if resp.status_code in (401, 403):
            raise AuthError(f"ingest rejected credentials: HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError:
            data = {}
        self.last = data if isinstance(data, dict) else {}  # the site's answer, e.g. {"review": "published"}
        if resp.status_code in (200, 201) and data.get("status") in ("created", "duplicate", "blocked", "filtered"):
            return data["status"]
        raise IngestError(f"HTTP {resp.status_code}: {str(data)[:200]}")
