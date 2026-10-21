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
    assert merge.would(L, {"field": "email", "value": "new@x.org", "confidence": "high"}) == "queued"
    assert merge.would(L, {"field": "founded", "value": "1999", "confidence": "high"}) == "applied"
    assert merge.would(L, {"field": "founded", "value": "1999", "confidence": "medium"}) == "queued"
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
