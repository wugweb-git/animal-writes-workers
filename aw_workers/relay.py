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


def fetch(url: str) -> str | None:
    """The page or feed as the site sees it, or None (not on the list, refused, or the site failed)."""
    if not allowed(url):
        return None
    try:
        resp = _state["client"].session.get(_state["client"].url.replace("/ingest", "/relay"), params={"url": url}, timeout=60)
        if resp.status_code != 200:
            log.warning("relay refused %s: HTTP %s %s", url, resp.status_code, resp.text[:200])
            return None
        data = resp.json()
        if int(data.get("status") or 0) >= 400:
            log.warning("relay: %s answered the site with HTTP %s", url, data.get("status"))
            return None
        raw = base64.b64decode(data.get("body") or "")
        m = re.search(r"charset=([\w-]+)", data.get("content_type") or "", re.I)
        try:
            text = raw.decode(m.group(1) if m else "utf-8", errors="replace")
        except LookupError:
            text = raw.decode("utf-8", errors="replace")
        log.info("relay: %s read through the site (%d bytes)", url, len(raw))
        return text
    except Exception as exc:  # the relay must never stop a run
        log.warning("relay failed for %s: %s", url, exc)
        return None
