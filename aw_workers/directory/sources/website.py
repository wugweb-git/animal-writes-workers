"""The organisation's own website: home, contact, about, donate, volunteer and adopt pages (8 at most).

The site counts only when its pages name the organisation. From it: email, phones (WhatsApp and emergency numbers where
the page says so), social profiles, donate / volunteer / adopt links, UPI ID, registration numbers (80G, 12A, FCRA,
Darpan ID, registration), year established, address and hours from its structured data, logo and photos; its text goes
to the AI pass (services, animals, areas served, description)."""

from __future__ import annotations

import json
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from ..model import BROWSER_UA, Fact, core_tokens, names_in_text, norm_phone, norm_url, pincode_in

KEY, NAME, HOST = "website", "The organisation's website", ""
MAX_PAGES = 8
PAGE_WORDS = re.compile(r"contact|about|who-we-are|donat|contribut|appeal|support-us|volunteer|get-involved|join|adopt|reach|helpline", re.I)
SOCIAL_HOSTS = ("instagram.com", "facebook.com", "fb.com", "twitter.com", "x.com", "youtube.com", "youtu.be", "linkedin.com",
                "wa.me", "whatsapp.com", "linktr.ee")
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
EMAIL_JUNK = re.compile(r"example\.|sentry|wixpress|\.(png|jpe?g|gif|webp|svg)$|@2x|domain\.com|email\.com|yourmail|godaddy", re.I)
PHONE = re.compile(r"(?:\+?91[\s-]?)?(?:\(?0\d{2,4}\)?[\s-]?\d{3,4}[\s-]?\d{3,4}|[6-9]\d{4}[\s-]?\d{5}|1800[\s-]?\d{3}[\s-]?\d{3,4})")
UPI = re.compile(r"\b[\w.\-]{2,64}@(?:ok(?:axis|hdfcbank|icici|sbi)|ybl|ibl|axl|paytm|upi|apl|sbi|icici|hdfcbank|kotak|barodampay|pnb|unionbank|indus|federal|idfcbank|rbl|aubank|yesbank|airtel|jio|fbl|axisbank|cnrb|boi|mahb)\b", re.I)
REG = {
    "reg_80g": re.compile(r"\b80\s?-?G\b[^\n]{0,40}?(?:no\.?|number|:)\s*[:\-]?\s*([A-Z0-9][A-Z0-9/\-().]{5,60}\d)", re.I),
    "reg_12a": re.compile(r"\b12\s?-?A{1,2}\b[^\n]{0,40}?(?:no\.?|number|:)\s*[:\-]?\s*([A-Z0-9][A-Z0-9/\-().]{5,60}\d)", re.I),
    "fcra": re.compile(r"\bFCRA\b[^\n]{0,40}?(?:no\.?|number|:)\s*[:\-]?\s*(\d{9})\b", re.I),
    "darpan_id": re.compile(r"\b([A-Z]{2}/\d{4}/\d{7})\b"),
    "registration": re.compile(r"\breg(?:istration|d)?\.?\s*(?:no|number)\.?\s*[:\-]?\s*([A-Z0-9][A-Z0-9/\-.]{3,40}\d)", re.I),
}
FOUNDED = re.compile(r"\b(?:established|founded|started|formed|set up|since)\s+(?:in\s+)?(?:the\s+year\s+)?(18[5-9]\d|19\d\d|20[0-2]\d)\b", re.I)
REG_TYPE = re.compile(r"\bregistered\s+(?:as\s+an?\s+|under\s+the\s+)?(public charitable trust|charitable trust|trust|society|section\s?8 (?:company|not[- ]for[- ]profit company))\b", re.I)
LINK_FIELD = [("donate_url", re.compile(r"donat|contribut|razorpay|instamojo|ketto|milaap|give\.do|payu", re.I)),
              ("volunteer_url", re.compile(r"volunteer", re.I)), ("adopt_url", re.compile(r"\badopt", re.I))]
PHONE_WORD = re.compile(r"phone|call|mobile|mob\b|tel\b|telephone|contact|helpline|whats\s?app|ph\b|ph\.|cell|emergency|ambulance|☎|📞", re.I)
BANK_WORD = re.compile(r"a/c|account|acc\.?\s?no|ifsc|bank|branch|pan\b|gst|cin\b|reg(?:istration|d)?\.?\s?no", re.I)
EMERGENCY = re.compile(r"emergency|ambulance|rescue (?:helpline|line|number)|24\s?[x×/]\s?7", re.I)


def social_profile(url: str) -> tuple[str, str] | None:
    """(field, clean URL) for a profile link; None for share buttons, posts and other non-profile links."""
    u = urlparse(url)
    host = (u.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    path = [p for p in u.path.split("/") if p]
    if not path:
        return None
    first = path[0].lower()
    if host == "instagram.com" and first not in ("p", "reel", "reels", "explore", "accounts", "stories", "tv"):
        return "instagram", f"https://www.instagram.com/{path[0]}/"
    if host in ("facebook.com", "fb.com") and first not in ("sharer", "sharer.php", "plugins", "tr", "dialog", "share", "login", "photo.php", "events", "groups", "hashtag"):
        if first == "profile.php":
            return ("facebook", f"https://www.facebook.com/profile.php?{u.query}") if "id=" in u.query else None
        return "facebook", f"https://www.facebook.com/{path[0]}"
    if host in ("twitter.com", "x.com") and first not in ("intent", "share", "home", "search", "hashtag", "i"):
        return "x", f"https://x.com/{path[0]}"
    if host == "youtube.com" and (first in ("channel", "c", "user") and len(path) > 1 or first.startswith("@")):
        return "youtube", "https://www.youtube.com/" + "/".join(path[:2] if not first.startswith("@") else path[:1])
    if host == "linkedin.com" and first in ("company", "school") and len(path) > 1:
        return "linkedin", f"https://www.linkedin.com/{first}/{path[1]}/"
    return None


def _jsonld(soup) -> list[dict]:
    out = []
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or "")
        except (ValueError, TypeError):
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            d = stack.pop()
            if isinstance(d, dict):
                if "@graph" in d:
                    stack += d["@graph"] if isinstance(d["@graph"], list) else [d["@graph"]]
                t = d.get("@type")
                t = " ".join(t) if isinstance(t, list) else str(t or "")
                if re.search(r"Organization|NGO|LocalBusiness|AnimalShelter|VeterinaryCare|Charity", t):
                    out.append(d)
    return out


def _ld_hours(spec) -> list[dict]:
    rows = []
    for s in spec if isinstance(spec, list) else [spec]:
        if not isinstance(s, dict):
            continue
        days = s.get("dayOfWeek") or []
        for d in days if isinstance(days, list) else [days]:
            d = str(d).rsplit("/", 1)[-1].capitalize()
            if d in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday") and s.get("opens") and s.get("closes"):
                rows.append({"day": d, "open": str(s["opens"])[:5], "close": str(s["closes"])[:5], "closed": False})
    return rows


def _text(soup) -> str:
    for t in soup(["script", "style", "noscript", "svg", "iframe"]):
        t.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ")).strip()


def _https_img(base: str, src: str) -> str:
    if not src or src.startswith("data:"):
        return ""
    u = urljoin(base, src.strip())
    return u if u.startswith("https://") and not re.search(r"\.(svg|gif)(\?|$)", u, re.I) else ""


def fetch(http, url: str):
    r = http.get(url, gap=1.0, headers={"User-Agent": BROWSER_UA, "Accept-Language": "en-IN,en;q=0.9"}, allow_redirects=True)
    if r.status_code != 200 or "html" not in r.headers.get("content-type", "html"):
        return None, url
    return r.text[:1_500_000], r.url


def run(ctx, env, http) -> None:
    cands = [ctx.leads.get("website"), (ctx.listing.get("now") or {}).get("website")]
    cands = [norm_url(c) for c in cands if c and not any(h in c for h in SOCIAL_HOSTS)]
    if not cands:
        ctx.status[KEY] = "none: no website known"
        return
    home, page, final = None, None, ""
    for c in dict.fromkeys(cands):
        try:
            page, final = fetch(http, c)
        except Exception as exc:  # noqa: BLE001
            ctx.status[KEY] = f"error: {type(exc).__name__}"
            continue
        if page:
            soup = BeautifulSoup(page, "html.parser")
            title = (soup.title.string if soup.title and soup.title.string else "")
            domain = (urlparse(final).hostname or "").lower()
            text = title + " " + _text(BeautifulSoup(page, "html.parser"))
            if names_in_text(ctx.name, text) >= 0.6 or any(t in domain for t in core_tokens(ctx.name) if len(t) >= 5):
                home = final
                break
            ctx.status[KEY] = "website does not name the organisation"
    if not home:
        ctx.status.setdefault(KEY, "error: unreadable")
        return
    origin = "{0.scheme}://{0.netloc}".format(urlparse(home))
    ctx.add(Fact("website", norm_url(home) if urlparse(home).path not in ("", "/") else origin, KEY, NAME, home))
    pages, queue, seen = [(home, page)], [], {home}
    soup = BeautifulSoup(page, "html.parser")
    for a in soup.find_all("a", href=True):
        u = urljoin(home, a["href"]).split("#")[0]
        if urlparse(u).netloc == urlparse(home).netloc and u not in seen and PAGE_WORDS.search(u + " " + a.get_text(" ")):
            seen.add(u)
            queue.append(u)
    for u in queue[: MAX_PAGES - 1]:
        try:
            p, f = fetch(http, u)
        except Exception:  # noqa: BLE001
            continue
        if p:
            pages.append((f, p))
    found_any = False
    for url, raw in pages:
        found_any |= _extract(ctx, url, raw, home)
    ctx.status[KEY] = "ok" if found_any else "ok: nothing new"


def _add(ctx, field, value, url, note=""):
    ctx.add(Fact(field, value, KEY, NAME, url, note=note))


def _extract(ctx, url: str, raw: str, home: str) -> bool:
    before = len(ctx.facts)
    soup = BeautifulSoup(raw, "html.parser")
    domain = (urlparse(home).hostname or "").lower().removeprefix("www.")
    # links: email, phone, WhatsApp, social, donate / volunteer / adopt
    for a in soup.find_all("a", href=True):
        h, label = a["href"].strip(), a.get_text(" ", strip=True)
        if h.lower().startswith("mailto:"):
            e = h[7:].split("?")[0].strip()
            if EMAIL.fullmatch(e) and not EMAIL_JUNK.search(e):
                _add(ctx, "email", e.lower(), url)
        elif h.lower().startswith("tel:"):
            p = norm_phone(h[4:])
            if p:
                prev = a.find_previous(string=True)
                ctx_near = label + " " + (str(prev)[-60:] if prev else "")  # the link's own words and just before it
                _add(ctx, "emergency" if EMERGENCY.search(ctx_near) else "phone", p, url)
        elif re.search(r"wa\.me/|api\.whatsapp\.com/send|whatsapp://send", h):
            m = re.search(r"(?:wa\.me/|phone=)\+?(\d{10,13})", h)
            if m and norm_phone(m.group(1)):
                _add(ctx, "whatsapp", norm_phone(m.group(1)), url)
        else:
            full = urljoin(url, h)
            sp = social_profile(full)
            if sp:
                _add(ctx, sp[0], sp[1], url)
                ctx.leads.setdefault(sp[0], sp[1])
                continue
            for field, pat in LINK_FIELD:
                if full.startswith("https://") and (pat.search(label) or pat.search(urlparse(full).path + urlparse(full).netloc)) and len(label) < 60:
                    _add(ctx, field, full, url)
                    break
    for ld in _jsonld(soup):
        tel = ld.get("telephone")
        for t in tel if isinstance(tel, list) else [tel]:
            if t and norm_phone(str(t)):
                _add(ctx, "phone", norm_phone(str(t)), url)
        if isinstance(ld.get("email"), str) and EMAIL.fullmatch(ld["email"].removeprefix("mailto:")):
            _add(ctx, "email", ld["email"].removeprefix("mailto:").lower(), url)
        addr = ld.get("address")
        if isinstance(addr, dict):
            line = ", ".join(str(addr.get(k)) for k in ("streetAddress", "addressLocality", "addressRegion", "postalCode") if addr.get(k))
            if pincode_in(line):
                _add(ctx, "address", line, url)
                _add(ctx, "pincode", pincode_in(line), url)
        geo = ld.get("geo")
        if isinstance(geo, dict) and geo.get("latitude"):
            try:
                _add(ctx, "lat", round(float(geo["latitude"]), 6), url)
                _add(ctx, "lng", round(float(geo["longitude"]), 6), url)
            except (TypeError, ValueError):
                pass
        rows = _ld_hours(ld.get("openingHoursSpecification") or [])
        if rows:
            _add(ctx, "hours", rows, url)
        logo = ld.get("logo")
        logo = logo.get("url") if isinstance(logo, dict) else logo
        if isinstance(logo, str) and _https_img(url, logo):
            _add(ctx, "logo", _https_img(url, logo), url)
        if str(ld.get("foundingDate", ""))[:4].isdigit():
            _add(ctx, "founded", str(ld["foundingDate"])[:4], url)
    # logo from the header; photos from og:image and large pictures
    if not any(f.field == "logo" and f.source_key == KEY for f in ctx.facts):
        for img in soup.find_all("img")[:40]:
            attrs = " ".join(str(img.get(k, "")) for k in ("src", "class", "alt", "id"))
            if re.search(r"logo", attrs, re.I):
                src = _https_img(url, img.get("src") or img.get("data-src") or "")
                if src:
                    _add(ctx, "logo", src, url)
                    break
    photos = []
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        photos.append(_https_img(url, og["content"]))
    for img in soup.find_all("img"):
        src = _https_img(url, img.get("src") or img.get("data-src") or "")
        w = re.sub(r"\D", "", str(img.get("width", "")))
        if src and not re.search(r"logo|icon|avatar|banner|head?er|hed(?:e)?r|menu|pipe|line|topbottom|bg|background|sprite|flag|payment|qr|button|arrow|shloka", src + " " + str(img.get("class", "")), re.I) and (not w or int(w) >= 250):
            photos.append(src)
    photos = [p for p in dict.fromkeys(photos) if p][:6]
    if photos:
        _add(ctx, "photos", photos, url)
    text = _text(soup)
    text = re.sub(r"\s*[\[\(]\s*at\s*[\]\)]\s*", "@", text, flags=re.I)  # "info [at] example.org"
    text = re.sub(r"\s*[\[\(]\s*dot\s*[\]\)]\s*", ".", text, flags=re.I)
    ctx.texts.append((KEY, url, text[:4000]))
    for e in EMAIL.findall(text):
        if not EMAIL_JUNK.search(e) and (domain in e.lower() or re.search(r"@(gmail|yahoo|rediffmail|hotmail|outlook)\.", e, re.I)):
            _add(ctx, "email", e.lower(), url)
    for m in PHONE.finditer(text):
        p = norm_phone(m.group(0))
        near = text[max(0, m.start() - 60): m.start()]
        # a number in plain text counts only when the words before it call it a phone (never an account number)
        if p and PHONE_WORD.search(near[-40:]) and not BANK_WORD.search(near) and not re.match(r"^0[01]", p):
            _add(ctx, "whatsapp" if re.search(r"whats\s?app", near, re.I) else "emergency" if EMERGENCY.search(near) else "phone", p, url)
    for u in UPI.findall(text):
        _add(ctx, "upi", u, url)
    for field, pat in REG.items():
        m = pat.search(text)
        if m:
            _add(ctx, field, m.group(1).strip(" .-"), url)
    m = FOUNDED.search(text)
    if m:
        _add(ctx, "founded", m.group(1), url)
    m = REG_TYPE.search(text)
    if m:
        t = m.group(1).lower()
        _add(ctx, "reg_type", "Section 8 company" if "section" in t else "Trust" if "trust" in t else "Society", url)
    return len(ctx.facts) > before
