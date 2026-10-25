"""Wikidata items are used only with name + same place, or a shared phone / email / website (owner, 25 Oct 2026); website
pictures are taken full size, not as WordPress thumbnails."""

from aw_workers.directory.model import Context, Fact
from aw_workers.directory.sources import website, wikidata


def claim(prop, value):
    return {"mainsnak": {"datavalue": {"value": value}}}


def item(**props):
    return {"claims": {p: [claim(p, v) for v in (vs if isinstance(vs, list) else [vs])] for p, vs in props.items()}}


def place(label, aliases=(), up=None):
    e = {"labels": {"en": {"value": label}}, "aliases": {"en": [{"value": a} for a in aliases]}, "claims": {}}
    if up:
        e["claims"]["P131"] = [claim("P131", {"id": up})]
    return e


PLACES = {"Q1": place("Udaipur", up="Q2"), "Q2": place("Udaipur district", up="Q3"), "Q3": place("Rajasthan"),
          "Q10": place("Pune", ("Poona",), up="Q11"), "Q11": place("Pune district", up="Q12"), "Q12": place("Maharashtra"),
          "Q20": place("Bangalore Urban district", up="Q21"), "Q21": place("Karnataka")}


class Resp:
    def __init__(self, data, status=200, ctype="application/json"):
        self.data, self.status_code, self.ok, self.headers = data, status, status == 200, {"content-type": ctype}

    def json(self):
        return self.data


class WikiHttp:
    def __init__(self, search, items):
        self.search, self.items, self.calls = search, {**PLACES, **items}, []

    def get(self, url, params=None, **kw):
        self.calls.append(params)
        if params["action"] == "wbsearchentities":
            return Resp({"search": self.search})
        return Resp({"entities": {i: self.items[i] for i in params["ids"].split("|") if i in self.items}})


def listing(**kw):
    base = {"id": 1, "name": "People for Animals", "district": "Udaipur", "state": "Rajasthan", "area": "", "now": {}, "claim": {}}
    base.update(kw)
    return base


def run(L, ent, facts=()):
    c = Context(L)
    c.facts += list(facts)
    http = WikiHttp([{"id": "Q99", "label": L["name"]}], {"Q99": ent})
    wikidata.run(c, {}, http)
    return c


INDIA = {"id": "Q668"}


def test_name_alone_is_never_enough():
    ent = item(P17=INDIA, P131={"id": "Q10"}, P856="https://pfapune.org", P571={"time": "+1996-00-00T00:00:00Z"})
    c = run(listing(), ent)
    assert not c.facts and c.status["wikidata"].startswith("name only")


def test_item_with_no_place_and_no_contact_is_not_used():
    c = run(listing(), item(P17=INDIA, P856="https://elsewhere.org"))
    assert not c.facts


def test_same_district_through_the_located_in_chain():
    ent = item(P17=INDIA, P131={"id": "Q1"}, P856="https://pfaudaipur.org")
    c = run(listing(), ent)
    assert ("website", "https://pfaudaipur.org") in {(f.field, f.value) for f in c.facts}
    assert "same district" in c.facts[0].note


def test_same_state_is_enough_with_the_name():
    ent = item(P17=INDIA, P131={"id": "Q3"}, P856="https://x.org")
    assert run(listing(district="Jodhpur"), ent).facts


def test_old_spelling_and_alias_count_as_the_same_place():
    ent = item(P17=INDIA, P159={"id": "Q10"}, P856="https://x.org")
    assert run(listing(district="Poona", state="Maharashtra"), ent).facts
    ent = item(P17=INDIA, P131={"id": "Q20"}, P856="https://y.org")
    assert run(listing(district="Bengaluru Urban", state=""), ent).facts


def test_shared_website_email_or_phone_counts_without_a_place():
    ent = item(P17=INDIA, P856="https://www.pfa-x.org/", P571={"time": "+1990-00-00T00:00:00Z"})
    c = run(listing(district="Kota", now={"website": "http://pfa-x.org"}), ent)
    assert {f.field for f in c.facts} >= {"wikidata", "founded", "website"} and "same website" in c.facts[0].note
    ent = item(P17=INDIA, P968="mailto:Help@PFA-x.org", P571={"time": "+1990-00-00T00:00:00Z"})
    assert run(listing(district="Kota", now={"email": "help@pfa-x.org"}), ent).facts
    ent = item(P17=INDIA, P1329="+91 98765 43210", P571={"time": "+1990-00-00T00:00:00Z"})
    assert run(listing(district="Kota", now={"phone": "98765 43210, 0294-2411111"}), ent).facts
    # another source's value in this run counts too, Wikidata's own never does
    assert run(listing(district="Kota"), ent, [Fact("phone", "98765 43210", "website", "W")]).facts
    c = run(listing(district="Kota"), ent, [Fact("phone", "98765 43210", "wikidata", "D")])
    assert [f.field for f in c.facts] == ["phone"] and c.status["wikidata"].startswith("name only")


def test_email_at_the_items_website_counts_but_free_mail_does_not():
    ent = item(P17=INDIA, P856="http://www.awcsindia.org/")
    assert run(listing(district="", state="", now={"email": "connect@awcsindia.org"}), ent).facts
    ent = item(P17=INDIA, P856="https://gmail.com")
    assert not run(listing(district="", state="", now={"email": "pfa@gmail.com"}), ent).facts


def test_former_places_do_not_count():
    old = {"mainsnak": {"datavalue": {"value": {"id": "Q30"}}}, "qualifiers": {"P582": [{}]}}
    dep = {"mainsnak": {"datavalue": {"value": {"id": "Q30"}}}, "rank": "deprecated"}
    PLACES["Q30"] = place("Madras Presidency", ("Chennai",))
    ent = {"claims": {"P17": [claim("P17", INDIA)], "P131": [old, dep], "P856": [claim("P856", "https://x.org")]}}
    assert not run(listing(district="Chennai", state="Tamil Nadu"), ent).facts


def test_coordinates_near_the_listing_count():
    ent = item(P17=INDIA, P625={"latitude": 24.58, "longitude": 73.71}, P856="https://x.org")
    assert run(listing(district="", state="", now={"lat": "24.60", "lng": "73.70"}), ent).facts
    assert not run(listing(district="", state="", now={"lat": "18.52", "lng": "73.85"}), ent).facts


def test_item_outside_india_is_skipped():
    assert not run(listing(), item(P17={"id": "Q145"}, P131={"id": "Q1"})).facts


# ---- pictures: full size, not WordPress thumbnails ----

class PicResp:
    def __init__(self, text, url, ctype):
        self.text, self.url, self.status_code, self.headers = text, url, 200, {"content-type": ctype}


class PicHttp:
    def __init__(self, pages, images):
        self.pages, self.images = pages, images

    def get(self, url, **kw):
        if url in self.images:
            return PicResp("", url, "image/png")
        if url.rstrip("/") in self.pages:
            return PicResp(self.pages[url.rstrip("/")], url, "text/html")
        return PicResp("", url, "text/html")  # a 404 page


def test_full_size_candidate():
    assert website.full_size_candidate("https://a.org/wp-content/uploads/2023/01/logo-150x150.png") == "https://a.org/wp-content/uploads/2023/01/logo.png"
    assert website.full_size_candidate("https://a.org/wp-content/uploads/dog-1024x683.jpg?v=2") == "https://a.org/wp-content/uploads/dog.jpg?v=2"
    assert website.full_size_candidate("https://a.org/img/dog.jpg") == ""


def test_website_pictures_are_full_size():
    up = "https://aps.example/wp-content/uploads/2022/05"
    home = f"""<html><head><title>Animal Protection Society Udaipur</title>
    <meta property="og:image" content="{up}/shelter-300x200.jpg"></head><body>
    <img class="custom-logo" src="{up}/aps-logo-30x30.png">
    <img src="{up}/rescue-150x150.jpg" srcset="{up}/rescue-150x150.jpg 150w, {up}/rescue-1024x683.jpg 1024w, {up}/rescue-768x512.jpg 768w">
    <img src="{up}/gone-150x150.jpg">
    <p>Animal Protection Society, Udaipur, Rajasthan.</p></body></html>"""
    c = Context({"id": 18188, "name": "Animal Protection Society", "district": "Udaipur", "state": "Rajasthan", "now": {}})
    c.leads["website"] = "https://aps.example"
    website.run(c, {}, PicHttp({"https://aps.example": home}, {f"{up}/aps-logo.png", f"{up}/shelter.jpg", f"{up}/rescue.jpg"}))
    got = {f.field: f.value for f in c.facts if f.field in ("logo", "photos")}
    assert got["logo"] == f"{up}/aps-logo.png"
    assert got["photos"] == [f"{up}/shelter.jpg", f"{up}/rescue.jpg", f"{up}/gone-150x150.jpg"]  # no full file: the thumbnail stays
