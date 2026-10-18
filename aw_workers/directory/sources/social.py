"""Public Instagram and Facebook pages: followers, posts, bio and contact details in the bio. Read as a link preview
(the page's own og: tags); pages that need a login give nothing and are reported as such. The bio text goes to the AI
pass. Instagram's latest post date needs a login, so it is not read."""

from __future__ import annotations

import html as htmllib
import re

from ..model import Fact, norm_phone

KEY, NAME, HOST = "social", "Instagram / Facebook", "instagram.com"
PREVIEW_UA = "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"
OG = re.compile(r'<meta[^>]+(?:property|name)=["\'](og:description|og:title|description)["\'][^>]+content=["\']([^"\']*)', re.I)
NUM = r"([\d.,]+\s?[KkMm]?)"


def count(s: str) -> str:
    s = s.replace(",", "").strip()
    mult = 1000 if s[-1:].lower() == "k" else 1_000_000 if s[-1:].lower() == "m" else 1
    try:
        return str(int(float(s.rstrip("KkMm ")) * mult))
    except ValueError:
        return ""


def meta(http, url: str) -> dict:
    r = http.get(url, gap=3.0, headers={"User-Agent": PREVIEW_UA, "Accept-Language": "en-US,en;q=0.9"}, allow_redirects=True)
    if r.status_code != 200 or "/accounts/login" in r.url or "login" in r.url.split("?")[0].rsplit("/", 2)[-2:]:
        return {}
    return {k.lower(): htmllib.unescape(v) for k, v in OG.findall(r.text[:300_000])}


def run(ctx, env, http) -> None:
    got, tried = [], []
    ig = ctx.leads.get("instagram")
    if ig:
        tried.append("instagram")
        m = meta(http, ig)
        d = m.get("og:description") or m.get("description") or ""
        f = re.search(NUM + r"\s+Followers?,\s*" + NUM + r"\s+Following,\s*" + NUM + r"\s+Posts?", d, re.I)
        if f:
            ctx.add(Fact("ig_followers", count(f.group(1)), KEY, "Instagram", ig), Fact("ig_posts", count(f.group(3)), KEY, "Instagram", ig))
            got.append("instagram")
        bio = re.sub(r"^.*?(?:on Instagram|Instagram photos and videos)[^:]*:\s*", "", d).strip(' "“”')
        if bio and bio != d:
            ctx.texts.append((KEY, ig, bio))
            _contacts(ctx, bio, ig, "Instagram")
    fb = ctx.leads.get("facebook")
    if fb:
        tried.append("facebook")
        m = meta(http, fb)
        d = m.get("og:description") or ""
        if d:
            got.append("facebook")
            ctx.texts.append((KEY, fb, d))
            _contacts(ctx, d, fb, "Facebook")
    if not tried:
        ctx.status[KEY] = "none: no social page known"
    else:
        ctx.status[KEY] = "ok: " + ", ".join(got) if got else "blocked: " + ", ".join(tried) + " gave nothing without a login"


def _contacts(ctx, text: str, url: str, name: str) -> None:
    for m in re.finditer(r"(?:\+?91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}", text):
        p = norm_phone(m.group(0))
        if p:
            near = text[max(0, m.start() - 40): m.start()]
            field = "whatsapp" if re.search(r"whats\s?app|wa\b", near, re.I) else "emergency" if re.search(r"emergency|ambulance|rescue", near, re.I) else "phone"
            ctx.add(Fact(field, p, KEY, name, url))
    for e in re.findall(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", text):
        ctx.add(Fact("email", e.lower(), KEY, name, url))
