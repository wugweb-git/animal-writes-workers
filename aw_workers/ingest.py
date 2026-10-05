"""POST items to the WordPress ingest endpoint."""

from __future__ import annotations

import requests

from .sources import Item

TIMEOUT = 30


class AuthError(Exception):
    """Credentials rejected. Stops the whole run."""


class IngestError(Exception):
    pass


def build_payload(item: Item, summary: str) -> dict:
    return {
        "type": item.source.type,
        "title": item.title,
        "summary": summary,
        "source_url": item.url,
        "source_name": item.source.name,
        "source_date": item.date.isoformat() if item.date else "",
        "topics": [],  # sent empty until the topic list is defined
    }


class Client:
    def __init__(self, base_url: str, user: str, app_password: str, session=None):
        self.url = base_url.rstrip("/") + "/wp-json/aw/v1/ingest"
        self.session = session or requests.Session()
        self.session.auth = (user, app_password)

    def post(self, payload: dict) -> str:
        """Returns 'created' or 'duplicate'."""
        resp = self.session.post(self.url, json=payload, timeout=TIMEOUT)
        if resp.status_code in (401, 403):
            raise AuthError(f"ingest rejected credentials: HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code in (200, 201) and data.get("status") in ("created", "duplicate"):
            return data["status"]
        raise IngestError(f"HTTP {resp.status_code}: {str(data)[:200]}")
