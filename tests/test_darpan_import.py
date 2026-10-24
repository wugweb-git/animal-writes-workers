"""Darpan CSV import: columns, matching with the same-state and same-district checks, proposals."""

from pathlib import Path

from aw_workers.directory import darpan_import as di

CSV = """NGO Name,Darpan Id,Registration No,Type of NPO,State,District,Mobile,Email,Website
Jeevan Jyoti Animal Welfare Society,DL/2019/0123456,S-1234,Society,Delhi,South,+91 98765 43210,care@jeevanjyoti.org,www.jeevanjyoti.org
Karuna Trust,DL/2018/0000001,T-22,Public Trust,Delhi,North,,,
Unknown Shelter,DL/2020/0000002,,Society,Delhi,East,,,
"""


def test_read_rows_maps_headers(tmp_path):
    p = tmp_path / "d.csv"
    p.write_text(CSV)
    rows = di.read_rows(p)
    assert rows[0]["darpan_id"] == "DL/2019/0123456" and rows[0]["district"] == "South" and rows[0]["state"] == "Delhi"
    assert di.reg_type_of("Public Trust") == "Trust" and di.reg_type_of("Societies Registration Act") == "Society"


def test_matching_checks_name_and_district():
    L = [{"id": 1, "name": "Jeevan Jyoti Animal Welfare Society", "district": "South", "darpan_id": ""},
         {"id": 2, "name": "Karuna Trust", "district": "South West", "darpan_id": ""}]
    row = {"name": "Jeevan Jyoti Animal Welfare Society", "district": "South", "state": "Delhi"}
    assert di.match(row, L)[0] == "ok"
    assert di.match({**row, "name": "Karuna Trust", "district": "North"}, L)[0] == "other district"  # same name, another district
    assert di.match({**row, "name": "Quite Different Name"}, L)[0] == "none"
    twin = [{"id": 1, "name": "Animal Care Trust", "district": "South", "darpan_id": ""}, {"id": 2, "name": "Animal Care Trust", "district": "South", "darpan_id": ""}]
    assert di.match({"name": "Animal Care Trust", "district": "South", "state": "X"}, twin)[0] == "ambiguous"


def test_proposals_carry_source_and_confidence():
    row = {"name": "x", "darpan_id": "DL/2019/0123456", "reg_no": "S-1234", "reg_type": "Society", "phone": "+91 98765 43210",
           "email": "Care@JeevanJyoti.org", "website": "www.jeevanjyoti.org", "state": "Delhi", "district": "South"}
    src = {"key": "darpan", "name": "NGO Darpan", "url": di.URL, "fetched_at": "2026-10-24T12:00:00+05:30"}
    got = {p["field"]: p for p in di.proposals(row, {"darpan_id": ""}, 0.95, src)}
    assert set(got) == {"darpan_id", "registration", "reg_type", "phone", "email", "website"}
    assert got["email"]["value"] == "care@jeevanjyoti.org" and got["phone"]["value"] == "98765 43210" and got["phone"]["confidence"] == "high"
    assert all(p["sources"][0]["fetched_at"] for p in got.values())
    assert "darpan_id" not in {p["field"] for p in di.proposals(row, {"darpan_id": "DL/2019/0123456"}, 0.95, src)}
