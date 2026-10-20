"""Fetch through the site (owner, 20 Oct 2026). Some publishers refuse GitHub's servers with 403 (PFA's Substack, Zoom TV)
but answer animalwrites.in. For a host on the site's relay list (Settings → Sourcing → Relay, sent with the sourcing
policy), a refused feed or page is asked for again through GET aw/v1/relay. The Google News site: search stays the last
resort for feeds."""

from __future__ import annotations

import base64
import logging
import re
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

_state: dict = {"client": None, "hosts": ()}


def configure(client, hosts) -> None:
    """The site client (its login) and the hosts the site will fetch for us."""
    _state["client"] = client
    _state["hosts"] = tuple(str(h).lower().strip(".") for h in (hosts or ()) if str(h).strip())


def allowed(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return bool(_state["client"]) and any(host == h or host.endswith("." + h) for h in _state["hosts"])


class Response:
    """What the site fetched, shaped like a requests response (status_code, url, content, text, headers)."""

    def __init__(self, status: int, url: str, content_type: str, content: bytes):
        self.status_code, self.url, self.content = status, url, content
        self.headers = {"content-type": content_type}
        m = re.search(r"charset=([\w-]+)", content_type or "", re.I)
        try:
            self.text = content.decode(m.group(1) if m else "utf-8", errors="replace")
        except LookupError:
            self.text = content.decode("utf-8", errors="replace")

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"{self.status_code} through the relay for url: {self.url}")


def get(url: str) -> Response | None:
    """The page, feed or file as the site sees it, or None (not on the list, refused, or the site failed)."""
    if not allowed(url):
        return None
    try:
        resp = _state["client"].session.get(_state["client"].url.replace("/ingest", "/relay"), params={"url": url}, timeout=60)
        if resp.status_code != 200:
            log.warning("relay refused %s: HTTP %s %s", url, resp.status_code, resp.text[:200])
            return None
        data = resp.json()
        out = Response(int(data.get("status") or 0), data.get("final_url") or url, data.get("content_type") or "",
                       base64.b64decode(data.get("body") or ""))
        log.info("relay: %s read through the site (HTTP %s, %d bytes)", url, out.status_code, len(out.content))
        return out
    except Exception as exc:  # the relay must never stop a run
        log.warning("relay failed for %s: %s", url, exc)
        return None


def fetch(url: str) -> str | None:
    """The page or feed's text as the site sees it, or None (also when the publisher answered the site with an error)."""
    resp = get(url)
    if resp is None:
        return None
    if resp.status_code >= 400:
        log.warning("relay: %s answered the site with HTTP %s", url, resp.status_code)
        return None
    return resp.text


class Session:
    """GET-only stand-in for a requests.Session whose requests all go through the site (no cookies, no POST)."""

    def __init__(self):
        self.headers: dict = {}

    def get(self, url: str, **_kw) -> Response:
        resp = get(url)
        if resp is None:
            import requests

            raise requests.ConnectionError(f"relay could not fetch {url}")
        return resp

    def post(self, url: str, **_kw):
        raise NotImplementedError("the relay only fetches pages (GET)")
