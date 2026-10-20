"""Resolve aggregator links to the publisher and find the article's preview image (og:image).

Only the image URL is sent; the site keeps its own copy (cc0-images.php, no hotlinking, owner 17–18 Oct 2026).
"""

from __future__ import annotations

import json
import re
from urllib.parse import urljoin, urlencode

import requests

UA = {"User-Agent": "Mozilla/5.0 (compatible; AnimalWritesWorkers/1.0; +https://animalwrites.in)"}
TIMEOUT = 25
_GN = re.compile(r"^https://news\.google\.com/(?:rss/)?articles/([^?/]+)")


def resolve_google_news(url: str, session=requests) -> str:
    """news.google.com article link -> publisher URL. Returns the input unchanged on any failure."""
    m = _GN.match(url)
    if not m:
        return url
    gid = m.group(1)
    try:
        page = session.get(f"https://news.google.com/rss/articles/{gid}", headers=UA, timeout=TIMEOUT).text
        sg = re.search(r'data-n-a-sg="([^"]+)"', page).group(1)
        ts = re.search(r'data-n-a-ts="([^"]+)"', page).group(1)
        inner = ["garturlreq", [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1, None, None, None, None, None, 0, 1],
                                "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0], gid, int(ts), sg]
        body = urlencode({"f.req": json.dumps([[["Fbv4je", json.dumps(inner), None, "generic"]]])})
        r = session.post("https://news.google.com/_/DotsSplashUi/data/batchexecute", data=body, timeout=TIMEOUT,
                         headers={**UA, "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"}).text
        found = re.search(r'garturlres\\",\\"(https?://.*?)\\"', r)
        if not found:
            return url
        link = found.group(1)
        for _ in range(3):  # the URL is JSON inside JSON: "=" and "&" arrive as \\u003d, \\u0026
            if "\\u" not in link:
                break
            link = link.encode().decode("unicode_escape")
        return link
    except Exception:
        return url


_OG = [re.compile(r'<meta[^>]+property=["\']og:image(?::secure_url)?["\'][^>]+content=["\']([^"\']+)', re.I),
       re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image', re.I),
       re.compile(r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)', re.I)]


# Link-preview crawlers first: social sites and many publishers give them the real picture (owner, 18 Oct 2026).
BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
UAS = ["facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)", "Twitterbot/1.0", BROWSER_UA]  # then a browser (PIB refuses bot-style agents)
PLATFORM_IMAGE = re.compile(r"static\.cdninstagram\.com|static\.xx\.fbcdn\.net|/rsrc\.php/|(instagram|facebook|threads|meta)[^/]*logo"
                            r"|/logo[^/]*\.(png|svg|webp|jpe?g)|-logo\.(png|webp|jpe?g)", re.I)
_PIB = re.compile(r"^https?://(www\.)?pib\.gov\.in/", re.I)
_PIB_IMG = re.compile(r'<img[^>]+src=["\'](https://static\.pib\.gov\.in/WriteReadData/userfiles/image/[^"\']+)', re.I)
_YT = re.compile(r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|embed/|live/)|youtu\.be/)([A-Za-z0-9_-]{11})")


_HEADLINES = [re.compile(r'<meta[^>]+property=["\']og:title["\'][^>]+content=(?:"([^"]*)"|\'([^\']*)\')', re.I),
              re.compile(r'<meta[^>]+content=(?:"([^"]*)"|\'([^\']*)\')[^>]+property=["\']og:title', re.I),
              re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S),
              re.compile(r"<h1[^>]*>(.*?)</h1>", re.I | re.S)]


def full_headline(url: str, cut: str, session=requests) -> str:
    """The page's own headline for a headline a feed cut off with "…" (owner, 20 Oct 2026): og:title, then <title>,
    then h1, the first that is the whole of `cut`; '' when none is (the title is then kept and flagged)."""
    from html import unescape

    from .titles import completes

    for ua in (BROWSER_UA, UAS[0]):
        try:
            html = session.get(url, headers={"User-Agent": ua}, timeout=TIMEOUT).text[:600_000]
        except Exception:
            continue
        for pat in _HEADLINES:
            m = pat.search(html)
            if not m:
                continue
            text = next((g for g in m.groups() if g is not None), "")
            text = re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", text))).strip()
            halves = text.split(" | ")
            if len(halves) == 2 and halves[0] == halves[1]:  # some sites print it twice
                text = halves[0]
            if completes(cut, text):
                return text
        if html:
            break
    return ""


def og_image(url: str, session=requests) -> str:
    """The page's declared preview image (https only, never a platform logo), or ''. YouTube: the video's cover."""
    if url.lower().endswith(".pdf"):
        return ""
    yt = _YT.search(url)
    if yt:
        return f"https://i.ytimg.com/vi/{yt.group(1)}/hqdefault.jpg"
    if _PIB.match(url):  # every PIB release declares the same site picture: the release's own first photo instead
        try:
            m = _PIB_IMG.search(session.get(url, headers={"User-Agent": UAS[-1]}, timeout=TIMEOUT).text)
            return m.group(1) if m else ""
        except Exception:
            return ""
    from . import relay

    for ua in UAS:
        try:
            resp = session.get(url, headers={"User-Agent": ua}, timeout=TIMEOUT)
            html = resp.text[:400_000]
        except Exception:
            continue
        relayed = getattr(resp, "status_code", 200) == 403 and relay.allowed(url)
        if relayed:  # refused from GitHub: the site reads the page for us (once)
            html = (relay.fetch(url) or "")[:400_000]
        for pat in _OG:
            m = pat.search(html)
            if m:
                img = urljoin(url, m.group(1).strip().replace("&amp;", "&"))
                if img.startswith("https://") and not PLATFORM_IMAGE.search(img):
                    return img
                break
        if relayed:
            break
    return ""
    try:
        resp = session.get(url, headers=UA, timeout=TIMEOUT)
        html = resp.text[:400_000]
    except Exception:
        return ""
    for pat in _OG:
        m = pat.search(html)
        if m:
            img = urljoin(url, m.group(1).strip().replace("&amp;", "&"))
            return img if img.startswith("https://") else ""
    return ""
