"""Directory enrichment: matching, parsing, and the confidence rules."""

from aw_workers.directory import ai, merge
from aw_workers.directory.model import CHECK, LEAD, Context, Fact, name_similarity, norm_phone, same_value
from aw_workers.directory.sources import google_places, osm, social, website


def ctx_with(*facts, now=None, protected=False):
    c = Context({"id": 1, "name": "Peela Ramakrishna Memorial Jeevraksha Sangham", "district": "Guntur",
                 "state": "Andhra Pradesh", "now": now or {}, "protected": protected})
    c.add(*facts)
    return c


def test_names_and_phones():
    assert name_similarity("Blue Cross of India", "Blue Cross Of India - Chennai") >= 0.9
    assert name_similarity("Animal Welfare Society Guntur", "People for Animals Guntur") < 0.75
    assert norm_phone("+91-98765 43210") == "98765 43210"
    assert norm_phone("(080) 2860 1985") == "08028601985"
    assert norm_phone("12345") == ""
    assert same_value("phone", "+91 9876543210", "098765-43210")
    assert same_value("website", "http://www.example.org/", "https://example.org")


def test_two_sources_agree_is_high_one_is_medium():
    c = ctx_with(Fact("email", "a@x.org", "website", "W"), Fact("email", "a@x.org", "osm", "O"),
                 Fact("founded", "1999", "website", "W"))
    props = {p["field"]: p for p in merge.proposals(c)[0]}
    assert props["email"]["confidence"] == "high"
    assert props["founded"]["confidence"] == "medium"


def test_conflict_is_low_and_claim_wins():
    c = ctx_with(Fact("email", "a@x.org", "website", "W"), Fact("email", "b@x.org", "osm", "O"))
    assert merge.proposals(c)[0][0]["confidence"] == "low"
    c = ctx_with(Fact("email", "b@x.org", "website", "W"), Fact("email", "c@x.org", "claim", "C"))
    p = merge.proposals(c)[0][0]
    assert (p["value"], p["confidence"]) == ("c@x.org", "high")


def test_google_only_confirms_or_invites():
    c = ctx_with(Fact("phone", "98765 43210", "website", "W"), Fact("phone", "+91 98765 43210", "google_places", "G", role=CHECK),
                 Fact("hours", [{"day": "Monday"}], "google_places", "G", role=CHECK),
                 Fact("website", "https://x.org", "google_places", "G", role=LEAD))
    props, invite = merge.proposals(c)
    assert [p["field"] for p in props] == ["phone"] and props[0]["confidence"] == "high"
    assert invite == ["hours"]  # Google is the only source of the hours; the website lead is never proposed


def test_ai_fields_capped_at_medium():
    c = ctx_with(Fact("services", ["rescue"], "website", "W"), Fact("services", ["rescue"], "claim", "C"),
                 Fact("animals", "dogs", "website", "W"), Fact("animals", "dogs", "social", "S"))
    props = {p["field"]: p for p in merge.proposals(c)[0]}
    assert props["services"]["confidence"] == "high"  # the organisation said so
    assert props["animals"]["confidence"] == "medium"


def test_would_apply_rules():
    L = {"now": {"email": "old@x.org"}, "protected": False}
    assert merge.would(L, {"field": "email", "value": "new@x.org", "confidence": "medium"}) == "applied"  # kept beside the old one
    assert merge.would(L, {"field": "founded", "value": "1999", "confidence": "medium"}) == "applied"  # an empty field, medium is enough
    assert merge.would(L, {"field": "founded", "value": "1999", "confidence": "low"}) == "queued"
    assert merge.would({**L, "now": {"founded": "1990"}}, {"field": "founded", "value": "1999", "confidence": "high"}) == "queued"
    assert merge.would({**L, "protected": True}, {"field": "founded", "value": "1999", "confidence": "high"}) == "queued"
    assert merge.would(L, {"field": "email", "value": "OLD@x.org", "confidence": "high"}) == "skipped"


def test_osm_hours():
    rows = osm.parse_hours("Mo-Fr 09:00-18:00; Sa 10:00-14:00; Su off")
    assert len(rows) == 7 and rows[0] == {"day": "Monday", "open": "09:00", "close": "18:00", "closed": False}
    assert rows[-1]["closed"] is True
    assert osm.parse_hours("sunrise-sunset") == []


def test_google_hours_and_match():
    spec = {"periods": [{"open": {"day": 1, "hour": 9, "minute": 0}, "close": {"day": 1, "hour": 17, "minute": 30}}]}
    assert google_places.hours_rows(spec) == [{"day": "Monday", "open": "09:00", "close": "17:30", "closed": False}]
    L = {"name": "Blue Cross of India", "district": "Chennai", "state": "Tamil Nadu"}
    good = {"displayName": {"text": "Blue Cross of India"}, "formattedAddress": "Velachery, Chennai, Tamil Nadu 600042"}
    far = {"displayName": {"text": "Blue Cross of India"}, "formattedAddress": "Hyderabad, Telangana"}
    assert google_places.place_matches(L, good) > 0.9 and google_places.place_matches(L, far) == 0


def test_social_profile_links():
    assert website.social_profile("https://www.instagram.com/bluecross/?hl=en") == ("instagram", "https://www.instagram.com/bluecross/")
    assert website.social_profile("https://www.facebook.com/sharer/sharer.php?u=x") is None
    assert website.social_profile("https://twitter.com/intent/tweet") is None
    assert website.social_profile("https://www.youtube.com/@bluecross") == ("youtube", "https://www.youtube.com/@bluecross")
    assert social.count("2.3K") == "2300" and social.count("1,204") == "1204"


class FakeResp:
    def __init__(self, text, url):
        self.text, self.url, self.status_code, self.headers = text, url, 200, {"content-type": "text/html"}


class FakeHttp:
    def __init__(self, pages):
        self.pages = pages

    def get(self, url, **kw):
        return FakeResp(self.pages.get(url.rstrip("/"), "<html></html>"), url)


def test_website_extracts_contacts():
    home = """<html><head><title>Jeevraksha Sangham Guntur</title><meta property="og:image" content="https://x.org/shelter.jpg"></head><body>
    <img class="site-logo" src="/logo.png"><a href="/contact-us">Contact</a><a href="https://rzp.io/l/abc">Donate now</a>
    <p>Peela Ramakrishna Memorial Jeevraksha Sangham was established in 1964. Registered as a public charitable trust.</p>
    <a href="https://www.instagram.com/jeevraksha/">Instagram</a></body></html>"""
    contact = """<html><body><a href="mailto:info@jeevraksha.org">Email</a><a href="tel:+919876543210">Call</a>
    <p>Emergency ambulance: 98490 12345</p><p>Bank A/c No: 0102578397, IFSC SBIN0001</p><p>80G No: AAATP1234QF20214 · UPI: jeevraksha@okhdfcbank</p>
    <a href="https://wa.me/919812345678">WhatsApp</a></body></html>"""
    c = ctx_with()
    c.leads["website"] = "https://jeevraksha.org"
    website.run(c, {}, FakeHttp({"https://jeevraksha.org": home, "https://jeevraksha.org/contact-us": contact}))
    got = {(f.field, str(f.value)) for f in c.facts}
    for want in [("website", "https://jeevraksha.org"), ("email", "info@jeevraksha.org"), ("phone", "98765 43210"),
                 ("emergency", "98490 12345"), ("whatsapp", "98123 45678"), ("founded", "1964"), ("reg_type", "Trust"),
                 ("upi", "jeevraksha@okhdfcbank"), ("donate_url", "https://rzp.io/l/abc"), ("logo", "https://jeevraksha.org/logo.png"),
                 ("instagram", "https://www.instagram.com/jeevraksha/"), ("reg_80g", "AAATP1234QF20214")]:
        assert want in got, want
    assert not any(v == "0102578397" for _, v in got)  # an account number is never a phone


def test_website_must_name_the_organisation():
    c = ctx_with()
    c.leads["website"] = "https://unrelated.example"
    website.run(c, {}, FakeHttp({"https://unrelated.example": "<html><title>Buy shoes</title><p>Shoes</p></html>"}))
    assert not c.facts and "does not name" in c.status["website"]


def test_ai_output_is_checked_against_the_text():
    text = "we rescue injured dogs and cats in guntur and vijayawada. founded by dr. ravi kumar, president."
    data = {"services": ["rescue", "flying"], "animals": ["dogs", "cats", "tigers"], "areas_served": ["Guntur", "Mumbai"],
            "description": "Rescues injured dogs and cats in Guntur, with 500 volunteers.", "head": "Ravi Kumar, President"}
    out = ai.checked(data, text, {"rescue": "Rescue"})
    assert out["services"] == ["rescue"] and out["animals"] == "dogs, cats" and out["areas_served"] == "Guntur"
    assert "description" not in out  # 500 is not in the text
    assert out["head"] == "Ravi Kumar, President"


def test_description_is_plain_and_short_and_services_are_named():
    text = "we rescue injured dogs in guntur and run an adoption drive every month."
    base = {"services": ["rescue", "adoption", "sterilisation"], "animals": ["dogs"]}
    svc = {"rescue": "Rescue", "adoption": "Adoption", "sterilisation": "Sterilisation (ABC)"}
    out = ai.checked({**base, "description": "Rescues injured dogs in Guntur and runs monthly adoption drives."}, text, svc)
    assert out["services"] == ["rescue", "adoption"]  # sterilisation is not in the text
    assert out["description"].startswith("Rescues")
    named = "Rescues injured dogs in Guntur; its dogs stay at All Creatures Great and Small."
    assert ai.checked({**base, "description": named}, text + " all creatures great and small", svc)["description"] == named
    for bad in ["A dedicated group that rescues injured dogs in Guntur.", "India's first rescue for dogs in Guntur.", "India’s first rescue for dogs in Guntur.",
                "We rescue injured dogs in Guntur.", " ".join(["dogs"] * 41)]:
        assert "description" not in ai.checked({**base, "description": bad}, text, svc), bad


def test_awbi_address_is_high_on_its_own():
    c = ctx_with(Fact("address", "Indira Nagar, Guntur 522003", "awbi", "A"), Fact("pincode", "522003", "awbi", "A"),
                 Fact("email", "a@x.org", "awbi", "A"))
    props = {p["field"]: p["confidence"] for p in merge.proposals(c)[0]}
    assert props == {"address": "high", "pincode": "high", "email": "medium"}


def test_file_leads_are_read():
    from aw_workers.directory import run

    assert run.file_leads(624) == {"website": "https://vibhastrust.org.in/"}
    assert run.file_leads(1) == {}


def test_lead_finder_keeps_only_own_pages():
    from aw_workers.directory import lead_finder as lf

    reply = '```json\n[{"id": 5, "website": "https://www.justdial.com/x", "instagram": "https://www.instagram.com/p/abc/", "facebook": "facebook.com/myngo"},' \
            ' {"id": 6, "website": "http://balmukund.org/contact_us.html", "instagram": "instagram.com/bal/?hl=en", "facebook": ""},' \
            ' {"id": 99, "website": "https://x.org"}]\n```'
    out = lf.parse(reply, {5, 6})
    assert out == {5: {"facebook": "https://www.facebook.com/myngo"}, 6: {"website": "https://balmukund.org", "instagram": "https://www.instagram.com/bal/"}}


def test_obfuscated_email_is_read():
    c = ctx_with()
    c.listing["name"] = "Balmukund Charitable Trust"
    c.leads["website"] = "https://balmukund.org"
    page = "<html><title>Balmukund Charitable Trust</title><body><p>Balmukund Charitable Trust, Guntur. Email :- info [at] balmukund.org</p></body></html>"
    website.run(c, {}, FakeHttp({"https://balmukund.org": page}))
    assert ("email", "info@balmukund.org") in {(f.field, f.value) for f in c.facts}


def test_district_sweep_matches_without_a_request():
    from aw_workers.directory.sources import google_places as gp

    places = [{"id": "A", "displayName": {"text": "Shri Pipardi Brahmeshwar Gaushala Trust"}, "formattedAddress": "Pipardi, Rajkot, Gujarat",
               "googleMapsUri": "https://maps.google.com/?cid=1", "websiteUri": "https://pipardi.example"}]
    c = Context({"id": 613, "name": "Shri Pipardi Brahmeshwar Gaushala Trust", "district": "Rajkot", "state": "Gujarat",
                 "_district_places": places, "_taken": set()})
    gp.run(c, {"GOOGLE_PLACES_KEY": "k"}, http=None)  # no http: it must not ask Google again
    assert c.leads["website"] == "https://pipardi.example" and c.google_cost == 0
    assert {f.field for f in c.facts} >= {"place_id", "maps_url"}
    c2 = Context({"id": 614, "name": "Shri Pipardi Brahmeshwar Gaushala Trust", "district": "Rajkot", "state": "Gujarat",
                  "_district_places": places, "_taken": {"A"}})
    assert gp.best(c2.listing, places, {"A"})[1] is None  # a place is matched to one listing only


def test_a_namesake_in_another_state_is_rejected():
    c = ctx_with()
    c.listing.update(name="Ashray Welfare Foundation", district="Lucknow", state="Uttar Pradesh")
    c.leads["website"] = "https://aashray.org.in"
    page = "<html><title>Aashray Welfare Foundation</title><body><p>Aashray Welfare Foundation, Baruipur, South 24 Parganas, West Bengal. Email: a@aashray.org.in</p></body></html>"
    website.run(c, {}, FakeHttp({"https://aashray.org.in": page}))
    assert not c.facts and c.status["website"].startswith("website does not")  # by name or by place


def test_one_shared_word_in_the_domain_is_not_enough():
    c = ctx_with()
    c.listing.update(name="Rashtriya Ahimsa Pratishthan", district="Jodhpur", state="Rajasthan")
    c.leads["website"] = "https://ahimsafellowship.org"
    page = "<html><title>Ahimsa Fellowship</title><body><p>Ahimsa Fellowship works for animals across India and Rajasthan. a@ahimsafellowship.org</p></body></html>"
    website.run(c, {}, FakeHttp({"https://ahimsafellowship.org": page}))
    assert not c.facts and c.status["website"].startswith("website does not name")


def test_social_link_from_website_and_its_own_preview_is_one_source():
    c = ctx_with(Fact("facebook", "https://www.facebook.com/x", "website", "W"), Fact("facebook", "https://www.facebook.com/x", "social", "S"))
    assert merge.proposals(c)[0][0]["confidence"] == "medium"
    c = ctx_with(Fact("facebook", "https://www.facebook.com/x", "website", "W"), Fact("facebook", "https://www.facebook.com/x", "wikidata", "D"))
    assert merge.proposals(c)[0][0]["confidence"] == "high"


def test_spelling_variants_and_bracketed_names():
    from aw_workers.directory.model import names_in_text

    assert names_in_text("Poona Panjrapole Trust", "Poona Panjarpol Trust, Pune since 1855") >= 0.6
    assert names_in_text("The Paws Pack (Resq Charitable Trust)", "RESQ Charitable Trust, Pune") >= 0.6
    assert names_in_text("Rashtriya Ahimsa Pratishthan", "Ahimsa Fellowship works for animals") < 0.6


def test_a_used_google_quota_asks_the_queue_again_without_google(monkeypatch, tmp_path):
    """30 and 31 Oct 2026: the same 25 listings waiting for Google came back every run once the month's quota was used."""
    from aw_workers.directory import run

    asked = []

    class Site:
        def __init__(self, *a, **k):
            pass

        def _call(self, method, path, **kw):
            if path == "/enrich/config":
                return {"sources": {}, "services": {}}
            if path == "/enrich/queue":
                asked.append(kw["params"]["google"])
                waiting = [{"id": 42, "name": "Waiting for Google", "state": "Odisha", "district": "Angul"}]
                return {"items": waiting if kw["params"]["google"] else [], "google_spent": 999}
            raise AssertionError(path)

    monkeypatch.setattr(run.ingest, "Client", Site)
    env = {"WP_BASE_URL": "https://animalwrites.in", "WP_INGEST_USER": "u", "WP_INGEST_APP_PASSWORD": "p",
           "GOOGLE_PLACES_KEY": "k", "GOOGLE_PLACES_FREE_REQUESTS": "950", "DRY_RUN": "false", "BATCH": "50",
           "REPORT_DIR": str(tmp_path)}
    run.main(env)
    assert asked == [1, 0]  # asked once with Google, then again without it; listing 42 was not checked


def test_a_results_timeout_is_tried_once_more_then_skipped(monkeypatch):
    """27 and 28 Oct 2026: one 504 on enrich/results stopped the whole run."""
    import pytest
    import requests

    from aw_workers.directory import run

    monkeypatch.setattr(run, "RESULTS_RETRY_WAIT", 0)

    class Site:
        def __init__(self, answers):
            self.answers, self.calls = list(answers), 0

        def _call(self, method, path, **kw):
            self.calls += 1
            a = self.answers.pop(0)
            if isinstance(a, int):
                resp = requests.Response()
                resp.status_code = a
                raise requests.HTTPError(f"{a} Server Error", response=resp)
            return a

    ok = Site([504, {"applied": ["email"]}])
    assert run.save_results(ok, {"listing_id": 1}) == {"applied": ["email"]} and ok.calls == 2
    gone = Site([504, 504])
    assert run.save_results(gone, {"listing_id": 2}) is None and gone.calls == 2
    with pytest.raises(requests.HTTPError):
        run.save_results(Site([500]), {"listing_id": 3})  # not a timeout: a real error still stops the run


def test_a_listing_not_saved_is_not_offered_again_in_the_same_run(monkeypatch, tmp_path):
    from aw_workers.directory import run

    monkeypatch.setattr(run, "save_results", lambda client, body: None)  # the site never saves it
    fetched = []

    class Site:
        def __init__(self, *a, **k):
            pass

        def _call(self, method, path, **kw):
            if path == "/enrich/config":
                return {"sources": {k: False for k in run.REGISTRY}, "services": {}}
            fetched.append(1)
            return {"items": [{"id": 7, "name": "Slow to save", "state": "Odisha", "district": "Angul"}], "google_spent": 0}

    monkeypatch.setattr(run.ingest, "Client", Site)
    env = {"WP_BASE_URL": "https://animalwrites.in", "WP_INGEST_USER": "u", "WP_INGEST_APP_PASSWORD": "p",
           "DRY_RUN": "false", "BATCH": "50", "REPORT_DIR": str(tmp_path)}
    assert run.main(env) == 0
    assert len(fetched) == 2  # the second fetch offers only listing 7 again: the run stops instead of retrying it
