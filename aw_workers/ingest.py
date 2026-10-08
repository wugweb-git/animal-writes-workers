"""POST items to the WordPress ingest endpoint."""

from __future__ import annotations

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
        "topics": topics if topics is not None else topics_for(item),
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

    def policy(self) -> dict:
        """Sourcing policy from WP Admin (News → Sourcing)."""
        resp = self.session.get(self.url.replace("/ingest", "/sourcing-policy"), timeout=TIMEOUT)
        if resp.status_code in (401, 403):
            raise AuthError(f"policy rejected credentials: HTTP {resp.status_code}")
        resp.raise_for_status()
        return resp.json()

    def post(self, payload: dict) -> str:
        """Returns 'created', 'duplicate', or 'blocked' / 'filtered' (refused by the site's sourcing policy)."""
        resp = self.session.post(self.url, json=payload, timeout=TIMEOUT)
        if resp.status_code in (401, 403):
            raise AuthError(f"ingest rejected credentials: HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code in (200, 201) and data.get("status") in ("created", "duplicate", "blocked", "filtered"):
            return data["status"]
        raise IngestError(f"HTTP {resp.status_code}: {str(data)[:200]}")
