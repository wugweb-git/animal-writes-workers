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


def run(ctx, env, http) -> None:
    key = (env.get("GOOGLE_PLACES_KEY") or "").strip()
    if not key:
        ctx.status[KEY] = "skipped: no GOOGLE_PLACES_KEY"
        return
    if env.get("_google_over_budget"):
        ctx.status[KEY] = "skipped: monthly budget reached"
        return
    L = ctx.listing
    query = " ".join(x for x in (L.get("name"), L.get("district"), L.get("state")) if x)
    ctx.google_cost += COST_USD
    resp = http.post(URL, gap=0.2, json={"textQuery": query, "regionCode": "IN", "languageCode": "en", "pageSize": 3},
                     headers={"X-Goog-Api-Key": key, "X-Goog-FieldMask": FIELDS, "Content-Type": "application/json"})
    if resp.status_code != 200:
        ctx.status[KEY] = f"error: HTTP {resp.status_code} {resp.text[:120]}"
        return
    places = resp.json().get("places") or []
    scored = sorted(((place_matches(L, p), p) for p in places), key=lambda x: -x[0])
    if not scored or scored[0][0] < 0.75:
        ctx.status[KEY] = "no match"
        return
    score, p = scored[0]
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
