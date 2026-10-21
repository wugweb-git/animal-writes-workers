"""Google Places (Text Search, New API): a locator and a check, never a source of stored facts (owner's brief).

Kept: the place ID, the Google Maps link, and the website Google lists (as a lead for the website module).
Compared only: phone, opening hours, address and position (a match raises another source's confidence); business status
(permanently closed: the listing is flagged). Secret GOOGLE_PLACES_KEY; spend is counted per request and capped by
GOOGLE_PLACES_MONTHLY_BUDGET_USD (run.py).
"""

from __future__ import annotations

from ..model import CHECK, LEAD, STORE, Fact, fold, name_similarity

KEY, NAME, HOST = "google_places", "Google Maps", "google.com"
URL = "https://places.googleapis.com/v1/places:searchText"
# Text Search with website, phone and hours in the field mask is billed as Text Search Enterprise.
COST_USD = 0.035
FIELDS = ",".join("places." + f for f in (
    "id", "displayName", "formattedAddress", "addressComponents", "googleMapsUri", "websiteUri", "nationalPhoneNumber",
    "businessStatus", "regularOpeningHours", "location"))
DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]


def hours_rows(spec: dict) -> list[dict]:
    """Google's periods as the site's rows: [{day, open, close, closed}]; days with no period are not stated."""
    rows = []
    for p in (spec or {}).get("periods") or []:
        o, c = p.get("open") or {}, p.get("close")
        if "day" not in o:
            continue
        if not c and o.get("hour", 0) == 0 and o.get("minute", 0) == 0:  # open 24 hours
            return [{"day": d, "open": "00:00", "close": "23:59", "closed": False} for d in DAYS[1:] + DAYS[:1]]
        rows.append({"day": DAYS[o["day"]], "open": f"{o.get('hour', 0):02d}:{o.get('minute', 0):02d}",
                     "close": f"{(c or {}).get('hour', 0):02d}:{(c or {}).get('minute', 0):02d}", "closed": False})
    return rows


def place_matches(listing: dict, place: dict) -> float:
    """Name similarity when the place is in the listing's district (or, without one, its state); else 0."""
    sim = name_similarity(listing.get("name", ""), (place.get("displayName") or {}).get("text", ""))
    where = fold(place.get("formattedAddress", "") + " " + " ".join(c.get("longText", "") for c in place.get("addressComponents") or []))
    district, state = fold(listing.get("district", "")).strip(), fold(listing.get("state", "")).strip()
    if district and district.split()[0] in where:
        return sim
    if not district and state and state in where:
        return sim
    if state and state in where and sim >= 0.9:  # district spelt differently (Bengaluru Urban / Bangalore)
        return sim - 0.05
    return 0.0


QUERIES = ["animal welfare organisation in {place}", "gaushala OR animal shelter OR veterinary in {place}"]


def sweep(env, http, state: str, district: str) -> tuple[list[dict], int]:
    """One district's worth of places (owner, 21 Oct 2026: search in bulk by district): a few Text Searches
    ("... in Rajkot, Gujarat"), 20 places each, with website and phone. Returns (places, requests used).
    Used to match many listings of the district at once instead of one search per listing."""
    key = (env.get("GOOGLE_PLACES_KEY") or "").strip()
    if not key or env.get("_google_over_budget"):
        return [], 0
    place = ", ".join(x for x in (district, state) if x)
    seen, out, used = set(), [], 0
    for q in QUERIES:
        resp = http.post(URL, gap=0.2, json={"textQuery": q.format(place=place), "regionCode": "IN", "languageCode": "en", "pageSize": 20},
                         headers={"X-Goog-Api-Key": key, "X-Goog-FieldMask": FIELDS, "Content-Type": "application/json"})
        used += 1
        if resp.status_code != 200:
            break
        for p in resp.json().get("places") or []:
            if p.get("id") not in seen:
                seen.add(p.get("id"))
                out.append(p)
    return out, used


def adopt(ctx, p: dict, score: float) -> None:
    """Take a matched place: its ID, Maps link and website (as a lead) are kept; the rest is only compared."""
    maps = p.get("googleMapsUri") or ""
    note = f"match {score:.2f}: {(p.get('displayName') or {}).get('text', '')}"
    ctx.add(Fact("place_id", p.get("id"), KEY, NAME, maps, STORE, note=note),
            Fact("maps_url", maps, KEY, NAME, maps, STORE, note=note))
    ctx.google_match = score
    if p.get("websiteUri"):
        ctx.add(Fact("website", p["websiteUri"], KEY, NAME, maps, LEAD))
        ctx.leads.setdefault("website", p["websiteUri"])
    if p.get("nationalPhoneNumber"):
        ctx.add(Fact("phone", p["nationalPhoneNumber"], KEY, NAME, maps, CHECK))
    if p.get("formattedAddress"):
        ctx.add(Fact("address", p["formattedAddress"], KEY, NAME, maps, CHECK))
    loc = p.get("location") or {}
    if "latitude" in loc:
        ctx.add(Fact("lat", round(loc["latitude"], 6), KEY, NAME, maps, CHECK), Fact("lng", round(loc["longitude"], 6), KEY, NAME, maps, CHECK))
    rows = hours_rows(p.get("regularOpeningHours") or {})
    if rows:
        ctx.add(Fact("hours", rows, KEY, NAME, maps, CHECK))
    if p.get("businessStatus") == "CLOSED_PERMANENTLY":
        ctx.closed = {"url": maps, "at": ctx.facts[-1].fetched_at}
    ctx.status[KEY] = "ok"


def best(listing: dict, places: list[dict], taken: set) -> tuple[float, dict | None]:
    scored = sorted(((place_matches(listing, p), p) for p in places if p.get("id") not in taken), key=lambda x: -x[0])
    return scored[0] if scored else (0.0, None)


def run(ctx, env, http) -> None:
    key = (env.get("GOOGLE_PLACES_KEY") or "").strip()
    if not key:
        ctx.status[KEY] = "skipped: no GOOGLE_PLACES_KEY"
        return
    L = ctx.listing
    score, p = best(L, L.get("_district_places") or [], L.get("_taken") or set())  # the district sweep first: no request
    if p is not None and score >= 0.75:
        L.get("_taken", set()).add(p.get("id"))
        adopt(ctx, p, score)
        ctx.status[KEY] = "ok (district search)"
        return
    if env.get("_google_over_budget"):
        ctx.status[KEY] = "skipped: monthly budget reached"
        return
    query = " ".join(x for x in (L.get("name"), L.get("district"), L.get("state")) if x)
    ctx.google_cost += COST_USD
    ctx.google_requests = getattr(ctx, "google_requests", 0) + 1
    resp = http.post(URL, gap=0.2, json={"textQuery": query, "regionCode": "IN", "languageCode": "en", "pageSize": 3},
                     headers={"X-Goog-Api-Key": key, "X-Goog-FieldMask": FIELDS, "Content-Type": "application/json"})
    if resp.status_code != 200:
        ctx.status[KEY] = f"error: HTTP {resp.status_code} {resp.text[:120]}"
        return
    score, p = best(L, resp.json().get("places") or [], set())
    if p is None or score < 0.75:
        ctx.status[KEY] = "no match"
        return
    adopt(ctx, p, score)
