"""Wikidata, for the larger organisations that have an item: founded, website, social profiles, position, logo.

Owner rule (25 Oct 2026): an item is used for a listing only when its name matches AND either
  - it is in the listing's state or district (the item's "located in" P131 chain, headquarters P159 or location P276,
    or its coordinates P625 within 25 km of the listing's own position), or
  - it shares a phone (P1329), email (P968) or website (P856) with what the listing already holds (its current values,
    an approved claim, or another source's values in this run); an email at the item's website domain counts as shared.
Places come from current claims only (none with an end time), so "British India" or "Madras Presidency" never match.
Never by name alone: a namesake elsewhere in India (or with no place on Wikidata) gives nothing."""

from __future__ import annotations

import math
import re
from urllib.parse import urlparse

from ..model import Fact, fold, name_similarity, norm_phone

KEY, NAME, HOST = "wikidata", "Wikidata", "wikidata.org"
API = "https://www.wikidata.org/w/api.php"
INDIA = "Q668"
PROPS = {"P856": "website", "P2003": "instagram", "P2013": "facebook", "P2002": "x", "P2397": "youtube", "P4264": "linkedin"}
SOCIAL = {"instagram": "https://www.instagram.com/{}/", "facebook": "https://www.facebook.com/{}", "x": "https://x.com/{}",
          "youtube": "https://www.youtube.com/channel/{}", "linkedin": "https://www.linkedin.com/company/{}/"}
PLACE_PROPS = ("P131", "P159", "P276")  # located in, headquarters, location
PLACE_DEPTH = 3  # locality -> district -> state
NEAR_KM = 25
PHONE_FIELDS = ("phone", "whatsapp", "emergency")
FREE_MAIL = {"gmail.com", "yahoo.com", "yahoo.co.in", "rediffmail.com", "hotmail.com", "outlook.com", "live.com", "icloud.com"}
HDR = {"User-Agent": "AnimalWritesWorkers/1.0 (https://animalwrites.in; admin@wugweb.com)"}
# Old and new spellings that Wikidata's own aliases sometimes miss.
PLACE_ALIAS = {"bangalore": "bengaluru", "bangalore urban": "bengaluru urban", "bangalore rural": "bengaluru rural",
               "bombay": "mumbai", "madras": "chennai", "calcutta": "kolkata", "gurgaon": "gurugram", "orissa": "odisha",
               "pondicherry": "puducherry", "nct of delhi": "delhi", "national capital territory of delhi": "delhi",
               "new delhi": "delhi", "trivandrum": "thiruvananthapuram", "poona": "pune", "baroda": "vadodara",
               "mysore": "mysuru", "cochin": "kochi", "ernakulam": "kochi", "uttaranchal": "uttarakhand",
               "allahabad": "prayagraj", "mumbai suburban": "mumbai", "mumbai city": "mumbai"}
_PLACE_WORDS = re.compile(r"\b(district|dist|distt|city|division|municipal corporation|metropolitan region|tehsil|taluk|taluka|state|union territory)\b")


def _vals(ent: dict, prop: str) -> list:
    out = []
    for c in (ent.get("claims") or {}).get(prop, []):
        dv = ((c.get("mainsnak") or {}).get("datavalue") or {}).get("value")
        if dv is not None:
            out.append(dv)
    return out


def _ids(ent: dict, props) -> list[str]:
    """Item ids of current claims: a deprecated claim or one with an end time (British India, Madras Presidency) is skipped."""
    out = []
    for p in props:
        for c in (ent.get("claims") or {}).get(p, []):
            v = ((c.get("mainsnak") or {}).get("datavalue") or {}).get("value")
            if isinstance(v, dict) and v.get("id") and c.get("rank") != "deprecated" and "P582" not in (c.get("qualifiers") or {}):
                out.append(v["id"])
    return out


def place_key(s: str) -> str:
    """A place name for matching: folded, without "district" / "city" and the like, old spellings to new."""
    k = re.sub(r"\s+", " ", _PLACE_WORDS.sub(" ", fold(s))).strip()
    return PLACE_ALIAS.get(k, k)


def _names(ent: dict) -> set[str]:
    out = {(ent.get("labels") or {}).get("en", {}).get("value", "")}
    out |= {a.get("value", "") for a in (ent.get("aliases") or {}).get("en", [])}
    return {place_key(n) for n in out if n} - {""}


def _host(u: str) -> str:
    u = (u or "").strip()
    if not u:
        return ""
    if not re.match(r"^https?://", u, re.I):
        u = "https://" + u
    return (urlparse(u).hostname or "").lower().removeprefix("www.")


def _email(e: str) -> str:
    return str(e or "").lower().removeprefix("mailto:").strip()


def _split(v) -> list[str]:
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v]
    return [x for x in re.split(r"[,;/]\s*", str(v or "")) if x.strip()]


def known_contacts(listing: dict, facts=()) -> dict:
    """The listing's own phones (last 10 digits), emails and website hosts: its current values, an approved claim's,
    and other sources' values from this run (never Wikidata's own)."""
    out = {"phone": set(), "email": set(), "website": set()}

    def put(field, value):
        for v in _split(value):
            if field in PHONE_FIELDS or field in ("aw_phone", "aw_whatsapp"):
                d = re.sub(r"\D", "", norm_phone(v))
                if len(d) >= 10:
                    out["phone"].add(d[-10:])
            elif field in ("email", "aw_email") and "@" in v:
                out["email"].add(_email(v))
            elif field in ("website", "aw_website") and _host(v):
                out["website"].add(_host(v))

    for src in (listing.get("now") or {}, listing.get("claim") or {}):
        for field, value in src.items():
            put(field, value)
    for f in facts:
        if f.source_key != KEY:
            put(f.field, f.value)
    return out


def shared_contact(ent: dict, known: dict) -> str:
    """Why the item is the listing's organisation by a shared phone / email / website ('' when none is shared)."""
    domains = {e.rsplit("@", 1)[-1] for e in known["email"]} - FREE_MAIL
    for v in _vals(ent, "P856"):
        h = _host(str(v))
        if h and h in known["website"]:
            return f"same website ({h})"
        if h and h in domains:
            return f"the listing's email is at the item's website ({h})"
    for v in _vals(ent, "P968"):
        if _email(str(v)) in known["email"]:
            return f"same email ({_email(str(v))})"
    for v in _vals(ent, "P1329"):
        d = re.sub(r"\D", "", str(v))
        if len(d) >= 10 and d[-10:] in known["phone"]:
            return f"same phone ({d[-10:]})"
    return ""


def place_names(ent: dict, fetch) -> set[str]:
    """Every name (label and aliases) of the places the item is in: P131 / P159 / P276 and up the P131 chain."""
    names, seen = set(), set()
    ids = _ids(ent, PLACE_PROPS)
    for _ in range(PLACE_DEPTH):
        ids = [i for i in dict.fromkeys(ids) if i not in seen and i != INDIA]
        if not ids:
            break
        seen |= set(ids)
        ents = fetch(ids)
        nxt = []
        for e in ents.values():
            names |= _names(e)
            nxt += _ids(e, ("P131",))
        ids = nxt
    return names


def _km(a, b) -> float:
    (la1, lo1), (la2, lo2) = a, b
    p = math.pi / 180
    h = math.sin((la2 - la1) * p / 2) ** 2 + math.cos(la1 * p) * math.cos(la2 * p) * math.sin((lo2 - lo1) * p / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


def same_place(listing: dict, ent: dict, names: set[str]) -> str:
    """Why the item is in the listing's place ('' when it is not, or Wikidata does not say)."""
    for label, key in (("district", "district"), ("state", "state"), ("area", "area")):
        k = place_key(listing.get(key) or "")
        if k and k in names:
            return f"same {label} ({listing.get(key)})"
    now = listing.get("now") or {}
    try:
        mine = (float(now["lat"]), float(now["lng"]))
    except (KeyError, TypeError, ValueError):
        mine = None
    if mine:
        for v in _vals(ent, "P625"):
            if isinstance(v, dict) and v.get("latitude") is not None and _km(mine, (v["latitude"], v["longitude"])) <= NEAR_KM:
                return f"within {NEAR_KM} km of the listing"
    return ""


def verdict(listing: dict, ent: dict, known: dict, fetch) -> tuple[bool, str]:
    """(usable, why) for an item whose name already matches: same place, or a shared phone / email / website."""
    why = shared_contact(ent, known)
    if why:
        return True, why
    why = same_place(listing, ent, place_names(ent, fetch))
    if why:
        return True, why
    return False, "name only: not in the listing's state or district, and no shared phone, email or website"


def _getter(http):
    def fetch(ids: list[str]) -> dict:
        out = {}
        for i in range(0, len(ids), 50):
            r = http.get(API, gap=0.5, headers=dict(HDR), params={"action": "wbgetentities", "ids": "|".join(ids[i:i + 50]),
                                                                   "props": "claims|labels|aliases", "languages": "en", "format": "json"})
            if getattr(r, "ok", r.status_code == 200):
                out.update(r.json().get("entities") or {})
        return out
    return fetch


def run(ctx, env, http) -> None:
    r = http.get(API, gap=0.5, headers=dict(HDR), params={"action": "wbsearchentities", "search": ctx.name, "language": "en",
                                                           "type": "item", "limit": 5, "format": "json"})
    hits = [h for h in (r.json().get("search") or []) if name_similarity(ctx.name, h.get("label", "")) >= 0.85] if r.ok else []
    if not hits:
        ctx.status[KEY] = "no match" if r.ok else f"error: HTTP {r.status_code}"
        return
    fetch = _getter(http)
    ents = fetch([h["id"] for h in hits[:3]])
    known = known_contacts(ctx.listing, ctx.facts)
    rejected = []
    for qid, ent in ents.items():
        countries = [v.get("id") for v in _vals(ent, "P17") if isinstance(v, dict)]
        if INDIA not in countries:
            continue
        ok, why = verdict(ctx.listing, ent, known, fetch)
        if not ok:
            rejected.append(qid)
            continue
        url = f"https://www.wikidata.org/wiki/{qid}"
        note = f"Wikidata match: {why}"
        ctx.add(Fact("wikidata", qid, KEY, NAME, url, note=note))
        for v in _vals(ent, "P571")[:1]:
            year = str(v.get("time", ""))[1:5]
            if year.isdigit():
                ctx.add(Fact("founded", year, KEY, NAME, url, note=note))
        for prop, field in PROPS.items():
            for v in _vals(ent, prop)[:1]:
                val = v if field == "website" else SOCIAL[field].format(v)
                ctx.add(Fact(field, val, KEY, NAME, url, note=note))
                ctx.leads.setdefault(field, val)
        for v in _vals(ent, "P625")[:1]:
            ctx.add(Fact("lat", round(v["latitude"], 6), KEY, NAME, url, note=note),
                    Fact("lng", round(v["longitude"], 6), KEY, NAME, url, note=note))
        for v in _vals(ent, "P154")[:1]:
            ctx.add(Fact("logo", "https://commons.wikimedia.org/wiki/Special:FilePath/" + str(v).replace(" ", "_"), KEY, NAME, url, note=note))
        ctx.status[KEY] = "ok"
        return
    ctx.status[KEY] = f"name only, not used: {', '.join(rejected)} (no shared place or contact)" if rejected else "no match"
