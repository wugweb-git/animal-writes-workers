"""POST items to the WordPress ingest endpoint."""

from __future__ import annotations

import os

import re

import requests

from .sources import Item, topics_for

TIMEOUT = 30


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


def build_payload(item: Item, summary: str, topics: list[str] | None = None) -> dict:
    payload = {
        "type": item.source.type,
        "title": item.title,
        "summary": summary,
        "source_url": item.url,
        "source_name": item.publisher or item.source.name,
        "source_date": item.date.isoformat() if item.date else "",
        "topics": (topics if topics is not None else topics_for(item)) + (["courts-judgments"] if item.source.source_type == "auto-court" else []),
        "feed": item.source.id,
    }
    source_type = court_type(item.title) if item.source.source_type == "auto-court" else item.source.source_type
    if source_type:
        payload["source_type"] = source_type
    if item.image:
        payload["image_url"] = item.image
    if item.doc_kind:  # Library documents: what it is, the court, and the PDF the site may keep a copy of
        payload["kind"] = item.doc_kind
        payload["document_url"] = item.document_url
        if item.court:
            payload["court"] = item.court
            payload.setdefault("source_type", item.court)
    if item.source.type == "opportunity":
        payload["org_name"] = item.source.org or item.source.name
        payload["place"] = item.place
        payload["deadline"] = item.deadline.isoformat() if item.deadline else ""
        payload["opportunity_type"] = item.source.opportunity_type
    return payload


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

    def policy(self) -> dict:
        """Sourcing policy from WP Admin (News → Sourcing)."""
        resp = self.session.get(self.url.replace("/ingest", "/sourcing-policy"), timeout=TIMEOUT)
        if resp.status_code in (401, 403):
            raise AuthError(f"policy rejected credentials: HTTP {resp.status_code}")
        resp.raise_for_status()
        return resp.json()

    def _call(self, method: str, path: str, **kw):
        resp = self.session.request(method, self.url.replace("/ingest", path), timeout=kw.pop("timeout", TIMEOUT), **kw)
        if resp.status_code in (401, 403):
            raise AuthError(f"{path} rejected credentials: HTTP {resp.status_code}")
        resp.raise_for_status()
        return resp.json()

    def relevance_config(self) -> dict:
        """Categories (aw_topic slug => name) and Directory organisation names, for the relevance gate."""
        return self._call("GET", "/relevance-config", timeout=90)

    def reject(self, entry: dict) -> dict:
        """Add an item the relevance gate refused to the site's Rejected log (Inbox → To review)."""
        return self._call("POST", "/relevance-rejected", json=entry)

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
                         errors: list[str] | None = None) -> dict:
        """Report this run: items created and published, sources tried and failed. The site emails the owner when half
        the sources fail; its own hourly check emails when nothing has been published for 12 hours or no run has
        reported for 8 hours (owner, 12 Oct 2026)."""
        run = os.environ.get("GITHUB_RUN_ID", "")
        link = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/{run}" if run else ""
        body = {"new": int(new), "link": link, "sources": int(sources), "sources_failed": int(sources_failed),
                "errors": [str(e)[:200] for e in (errors or [])][:10]}
        if published is not None:
            body["published"] = int(published)
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

    def seo_focus(self, items: list[dict]) -> dict:
        """Write focus keywords [{id, keyword}]; the site writes only where Rank Math's field is still empty."""
        return self._call("POST", "/seo-focus", json={"items": items}, timeout=90)

    def post(self, payload: dict) -> str:
        """Returns 'created', 'duplicate', or 'blocked' / 'filtered' (refused by the site's sourcing policy)."""
        resp = self.session.post(self.url, json=payload, timeout=TIMEOUT)
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
