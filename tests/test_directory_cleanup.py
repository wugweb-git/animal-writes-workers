"""Directory clean-up pass: places from the listing's own PIN code / address, and the strict duplicate rule."""

import json

from aw_workers.directory import cleanup

PLACES = {
    "states": [{"code": 1, "name": "Jammu and Kashmir"}, {"code": 29, "name": "Karnataka"}, {"code": 36, "name": "Telangana"},
               {"code": 28, "name": "Andhra Pradesh"}, {"code": 38, "name": "Ladakh"}],
    "districts": [{"code": 1, "state": 1, "name": "Jammu"}, {"code": 2, "state": 1, "name": "Srinagar"},
                  {"code": 525, "state": 29, "name": "Bengaluru Urban"}, {"code": 536, "state": 29, "name": "Mysuru"},
                  {"code": 600, "state": 36, "name": "Hyderabad"}, {"code": 700, "state": 28, "name": "Prakasam"},
                  {"code": 701, "state": 28, "name": "Guntur"}],
    "aliases": {"state": {"orissa": "odisha"}, "district": {"bangalore": "bengaluru urban", "mysore": "mysuru"}},
}


class FakeBook:
    def __init__(self, answers):
        self.answers, self.calls = answers, 0

    def lookup(self, pin):
        self.calls += 1
        return self.answers.get(pin, [])


def L(id_, **kw):
    base = {"id": id_, "name": f"Org {id_}", "url": f"https://x/{id_}/", "states": [], "districts": [], "types": []}
    base.update(kw)
    return base


P = cleanup.Places(PLACES)


def test_pin_gives_district_high():
    book = FakeBook({"523157": [("Andhra Pradesh", "Prakasam")]})
    f = cleanup.place_finding(L(1, states=[28], address="II, TD Colony, Chirala, 523157"), P, book)
    assert f["value"] == {"state": 28, "district": 700} and f["confidence"] == "high"
    assert f["sources"][0]["key"] == "india_post" and f["pin_from_address"]


def test_pin_in_another_state_is_low():
    book = FakeBook({"500094": [("Telangana", "Hyderabad")]})
    f = cleanup.place_finding(L(2, states=[28], address="B-153, Sainikpuri, 500094", area="Secunderabad, Andhra Pradesh"), P, book)
    assert f["confidence"] == "low" and f["value"] == {"state": 36, "district": 600}


def test_pin_against_the_listings_own_name_or_address_is_low():
    book = FakeBook({"500094": [("Telangana", "Hyderabad")]})
    f = cleanup.place_finding(L(9, name="Dog rescue, Karnataka", address="Plot 4, 500094"), P, book)
    assert f["confidence"] == "low" and "own name or address says Karnataka" in f["why"]
    f = cleanup.place_finding(L(10, address="Sainikpuri, Hyderabad, Telangana 500094"), P, book)
    assert f["confidence"] == "high"  # the address agrees with the PIN


def test_address_names_one_district_medium_and_aliases():
    f = cleanup.place_finding(L(3, states=[29], address="12 MG Road, Mysore"), P, FakeBook({}))
    assert f["value"] == {"state": 29, "district": 536} and f["confidence"] == "medium"


def test_state_name_is_not_a_district():
    f = cleanup.place_finding(L(4, states=[38], address="Leh, Ladakh, Jammu and Kashmir"), P, FakeBook({}))
    assert "value" not in f  # "Jammu and Kashmir" does not name the district Jammu


def test_two_districts_or_name_only():
    f = cleanup.place_finding(L(5, states=[28], address="Guntur Road, Prakasam"), P, FakeBook({}))
    assert "value" not in f and f["ambiguous"] == [700, 701]
    f = cleanup.place_finding(L(6, states=[29], name="SPCA Mysore"), P, FakeBook({}))
    assert f["confidence"] == "low"  # the name alone is not enough: review


def test_no_state_gets_state_from_text_and_districts_are_left_alone():
    f = cleanup.place_finding(L(7, address="Somewhere, Karnataka"), P, FakeBook({}))
    assert f["value"] == {"state": 29, "district": 0} and f["confidence"] == "medium"
    assert cleanup.place_finding(L(8, states=[29], districts=[525]), P, FakeBook({})) is None


def test_mvu_is_state_wide():
    assert cleanup.is_mvu(L(9, name="1962 Mobile Veterinary Unit, Karnataka", types=["mobile-veterinary-unit"]))


def test_duplicates_need_a_shared_contact():
    a = L(10, name="Lets Live Together", districts=[525], website="https://letslivetogether.org", phones=["98765 43210"], email="a@b.org")
    b = L(11, name="Lets live together", districts=[525], website="http://www.letslivetogether.org/")
    c = L(12, name="Lets Live Together", districts=[525], phones=["91234 56789"])  # same name, nothing shared
    merges, left = cleanup.duplicate_groups([a, b, c])
    assert len(merges) == 1 and merges[0]["keep"]["id"] == 10 and [x["id"] for x in merges[0]["others"]] == [11]
    assert left[0]["ids"] == [12]
    (lst, p), = cleanup.merge_proposals(merges[0])
    assert lst["id"] == 11 and p["field"] == "merge_into" and p["value"] == 10 and p["confidence"] == "medium"


def test_duplicates_with_own_awbi_codes_are_left_and_claimed_is_kept():
    a = L(20, name="Shri Gaushala", districts=[1], awbi_code="PJ016/1999", address="Village A, Tehsil B, District Jammu")
    b = L(21, name="Shri Gaushala", districts=[1], awbi_code="PJ064/2002", address="Village C, Tehsil D, District Jammu")
    merges, left = cleanup.duplicate_groups([a, b])
    assert not merges and "AWBI" in left[0]["why"]
    x = L(30, name="PFA", districts=[2], phones=["98765 43210"], email="x@y.org", website="https://pfa.org", protected=True)
    y = L(31, name="PFA", districts=[2], phones=["98765 43210"], email="x@y.org", website="https://pfa.org", facebook="f", instagram="i")
    merges, _ = cleanup.duplicate_groups([x, y])
    assert merges[0]["keep"]["id"] == 30  # claimed or verified listing kept
    assert cleanup.merge_proposals(merges[0])[0][1]["confidence"] == "high"


def test_social_links_and_short_numbers_do_not_count():
    a = L(40, name="X", districts=[1], website="https://facebook.com/x", phones=["1962"])
    b = L(41, name="X", districts=[1], website="https://facebook.com/x", phones=["1962"])
    merges, left = cleanup.duplicate_groups([a, b])
    assert not merges and left


def test_dry_run_from_file(tmp_path, monkeypatch):
    items = [L(1, states=[28], address="Chirala 523157"), L(10, name="A", districts=[525], email="a@b.org"),
             L(11, name="A", districts=[525], email="A@b.org")]
    f = tmp_path / "in.json"
    f.write_text(json.dumps({"items": items, "places": PLACES}))
    monkeypatch.setattr(cleanup.PinBook, "lookup", lambda self, pin: [("Andhra Pradesh", "Prakasam")])
    env = {"CLEANUP_FILE": str(f), "DRY_RUN": "false", "REPORT_DIR": str(tmp_path / "r")}  # no ENRICH_LIVE: still a dry run
    assert cleanup.main(env) == 0
    rep = json.loads((tmp_path / "r" / "report.json").read_text())
    assert rep["dry_run"] is True
    assert rep["place"][0]["value"] == {"state": 28, "district": 700}
    assert rep["merges"] == [{"district": 525, "name": "a", "keep": 10, "merge": [11], "shared": ["email"]}]
    assert rep["outcome"]["merge_into"]["queued"] == 1 and rep["outcome"]["pincode"]["applied"] == 1
