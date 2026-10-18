"""Wikidata, for the larger organisations that have an item: founded, website, social profiles, position, logo."""

from __future__ import annotations

from ..model import Fact, name_similarity

KEY, NAME, HOST = "wikidata", "Wikidata", "wikidata.org"
API = "https://www.wikidata.org/w/api.php"
INDIA = "Q668"
PROPS = {"P856": "website", "P2003": "instagram", "P2013": "facebook", "P2002": "x", "P2397": "youtube", "P4264": "linkedin"}
SOCIAL = {"instagram": "https://www.instagram.com/{}/", "facebook": "https://www.facebook.com/{}", "x": "https://x.com/{}",
          "youtube": "https://www.youtube.com/channel/{}", "linkedin": "https://www.linkedin.com/company/{}/"}


def _vals(ent: dict, prop: str) -> list:
    out = []
    for c in (ent.get("claims") or {}).get(prop, []):
        dv = ((c.get("mainsnak") or {}).get("datavalue") or {}).get("value")
        if dv is not None:
            out.append(dv)
    return out


def run(ctx, env, http) -> None:
    hdr = {"User-Agent": "AnimalWritesWorkers/1.0 (https://animalwrites.in; admin@wugweb.com)"}
    r = http.get(API, gap=0.5, headers=hdr, params={"action": "wbsearchentities", "search": ctx.name, "language": "en",
                                                     "type": "item", "limit": 5, "format": "json"})
    hits = [h for h in (r.json().get("search") or []) if name_similarity(ctx.name, h.get("label", "")) >= 0.85] if r.ok else []
    if not hits:
        ctx.status[KEY] = "no match" if r.ok else f"error: HTTP {r.status_code}"
        return
    ids = "|".join(h["id"] for h in hits[:3])
    ents = (http.get(API, gap=0.5, headers=hdr, params={"action": "wbgetentities", "ids": ids, "props": "claims|labels",
                                                          "format": "json"}).json().get("entities") or {})
    for qid, ent in ents.items():
        countries = [v.get("id") for v in _vals(ent, "P17") if isinstance(v, dict)]
        if INDIA not in countries:
            continue
        url = f"https://www.wikidata.org/wiki/{qid}"
        ctx.add(Fact("wikidata", qid, KEY, NAME, url))
        for v in _vals(ent, "P571")[:1]:
            year = str(v.get("time", ""))[1:5]
            if year.isdigit():
                ctx.add(Fact("founded", year, KEY, NAME, url))
        for prop, field in PROPS.items():
            for v in _vals(ent, prop)[:1]:
                val = v if field == "website" else SOCIAL[field].format(v)
                ctx.add(Fact(field, val, KEY, NAME, url))
                ctx.leads.setdefault(field, val)
        for v in _vals(ent, "P625")[:1]:
            ctx.add(Fact("lat", round(v["latitude"], 6), KEY, NAME, url), Fact("lng", round(v["longitude"], 6), KEY, NAME, url))
        for v in _vals(ent, "P154")[:1]:
            ctx.add(Fact("logo", "https://commons.wikimedia.org/wiki/Special:FilePath/" + str(v).replace(" ", "_"), KEY, NAME, url))
        ctx.status[KEY] = "ok"
        return
    ctx.status[KEY] = "no match"
