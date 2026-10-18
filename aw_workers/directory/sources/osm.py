"""OpenStreetMap (Overpass): position and the opening hours, phone, website and PIN code tagged on a matching place.
© OpenStreetMap contributors, ODbL: the site credits OpenStreetMap wherever these values show."""

from __future__ import annotations

import re
import time

from ..model import Fact, core_tokens, name_similarity

KEY, NAME, HOST = "osm", "OpenStreetMap", "openstreetmap.org"
ENDPOINTS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter",
             "https://overpass.private.coffee/api/interpreter"]
ISO = {
    "andaman and nicobar islands": "AN", "andhra pradesh": "AP", "arunachal pradesh": "AR", "assam": "AS", "bihar": "BR",
    "chandigarh": "CH", "chhattisgarh": "CT", "dadra and nagar haveli and daman and diu": "DH", "delhi": "DL", "goa": "GA",
    "gujarat": "GJ", "haryana": "HR", "himachal pradesh": "HP", "jammu and kashmir": "JK", "jharkhand": "JH",
    "karnataka": "KA", "kerala": "KL", "ladakh": "LA", "lakshadweep": "LD", "madhya pradesh": "MP", "maharashtra": "MH",
    "manipur": "MN", "meghalaya": "ML", "mizoram": "MZ", "nagaland": "NL", "odisha": "OR", "puducherry": "PY",
    "punjab": "PB", "rajasthan": "RJ", "sikkim": "SK", "tamil nadu": "TN", "telangana": "TG", "tripura": "TR",
    "uttar pradesh": "UP", "uttarakhand": "UK", "west bengal": "WB", "nct of delhi": "DL",
}
DAY = {"Mo": "Monday", "Tu": "Tuesday", "We": "Wednesday", "Th": "Thursday", "Fr": "Friday", "Sa": "Saturday", "Su": "Sunday"}
ORDER = list(DAY)


def parse_hours(spec: str) -> list[dict]:
    """Simple opening_hours ("Mo-Fr 09:00-18:00; Sa 10:00-14:00; Su off", "24/7") as day rows; [] when not simple."""
    spec = (spec or "").strip()
    if spec == "24/7":
        return [{"day": d, "open": "00:00", "close": "23:59", "closed": False} for d in DAY.values()]
    rows = []
    for part in [p.strip() for p in spec.split(";") if p.strip()]:
        m = re.fullmatch(r"((?:Mo|Tu|We|Th|Fr|Sa|Su)(?:-(?:Mo|Tu|We|Th|Fr|Sa|Su))?(?:,(?:Mo|Tu|We|Th|Fr|Sa|Su)(?:-(?:Mo|Tu|We|Th|Fr|Sa|Su))?)*)\s+(off|closed|(?:\d{2}:\d{2}-\d{2}:\d{2})(?:,\d{2}:\d{2}-\d{2}:\d{2})*)", part)
        if not m:
            return []
        days = []
        for rng in m.group(1).split(","):
            a, _, b = rng.partition("-")
            i, j = ORDER.index(a), ORDER.index(b or a)
            days += ORDER[i:j + 1] if i <= j else ORDER[i:] + ORDER[:j + 1]
        for d in days:
            if m.group(2) in ("off", "closed"):
                rows.append({"day": DAY[d], "open": "", "close": "", "closed": True})
            else:
                for span in m.group(2).split(","):
                    o, c = span.split("-")
                    rows.append({"day": DAY[d], "open": o, "close": c, "closed": False})
    return rows


def query(name: str, state: str) -> str:
    toks = sorted(core_tokens(name), key=len, reverse=True)[:2]
    if not toks:
        return ""
    iso = ISO.get(state.lower().strip())
    area = f'area["ISO3166-2"="IN-{iso}"]->.a;' if iso else 'area["ISO3166-1"="IN"]->.a;'
    flt = "".join(f'["name"~"{re.escape(t)}",i]' for t in toks)
    return f"[out:json][timeout:40];{area}nwr{flt}(area.a);out center tags 10;"


def run(ctx, env, http) -> None:
    q = query(ctx.name, ctx.listing.get("state", ""))
    if not q:
        ctx.status[KEY] = "skipped: no usable name"
        return
    data, err = None, ""
    for attempt, ep in enumerate(ENDPOINTS * 2):
        try:
            r = http.post(ep, gap=2.0, data={"data": q}, timeout=60,
                          headers={"User-Agent": "AnimalWritesWorkers/1.0 (+https://animalwrites.in; admin@wugweb.com)"})
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("application/json"):
                data = r.json()
                break
            err = f"HTTP {r.status_code}"
        except Exception as exc:  # noqa: BLE001
            err = type(exc).__name__
        http.sleep(min(30, 3 * 2 ** attempt))
    if data is None:
        ctx.status[KEY] = f"error: {err}"
        return
    best = max(((name_similarity(ctx.name, e.get("tags", {}).get("name", "")), e) for e in data.get("elements", [])),
               key=lambda x: x[0], default=(0, None))
    if best[0] < 0.8:
        ctx.status[KEY] = "no match"
        return
    e = best[1]
    t = e.get("tags", {})
    url = f"https://www.openstreetmap.org/{e['type']}/{e['id']}"
    lat, lon = e.get("lat", (e.get("center") or {}).get("lat")), e.get("lon", (e.get("center") or {}).get("lon"))
    if lat is not None:
        ctx.add(Fact("lat", round(lat, 6), KEY, NAME, url), Fact("lng", round(lon, 6), KEY, NAME, url))
    rows = parse_hours(t.get("opening_hours", ""))
    if rows:
        ctx.add(Fact("hours", rows, KEY, NAME, url))
    for tag, field in (("phone", "phone"), ("contact:phone", "phone"), ("email", "email"), ("contact:email", "email"),
                       ("website", "website"), ("contact:website", "website"), ("addr:postcode", "pincode"),
                       ("contact:instagram", "instagram"), ("contact:facebook", "facebook")):
        if t.get(tag):
            v = t[tag].split(";")[0].strip()
            ctx.add(Fact(field, v, KEY, NAME, url))
            if field in ("website", "instagram", "facebook"):
                ctx.leads.setdefault(field, v)
    ctx.status[KEY] = "ok"
