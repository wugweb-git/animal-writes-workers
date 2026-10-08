from datetime import date, datetime, timezone

from aw_workers import documents, ingest
from aw_workers.sources import Source


def src(kind, **kw):
    return Source(id="t", name="Test", kind=kind, type="legal", url="https://example.com/", **kw)


def test_about_animals():
    assert documents.about_animals("Prevention of Cruelty to Animals (Laboratory Animals) Rules, 2026")
    assert documents.about_animals("W.P.(C) 12/2026 People for Animals vs State")
    assert not documents.about_animals("Service matter: Department of Animal Husbandry and Dairying vs A. Kumar")
    assert not documents.about_animals("Anti Dumping Clavulanic Acid and its Amine Salt China PR")
    assert not documents.about_animals("WP/199/2018 of G Vs STATE OF MAHA. THR. DY. SEC. DEPT. OF AGRI. AND ANIMAL HUSBANDARY")
    assert not documents.about_animals("CRL.P/12334/2026 of MOHAMMADH SAIF @ TIGER SAIF Vs STATE OF KARNATAKA")
    assert documents.about_animals("Cont.Cas(C)/481/2026 of SHREE GEETA GAUSHALA PARAMARTH SANSTHA Vs K")


def test_supreme_court_rows():
    rows = [
        {"title": "STATE versus PEOPLE FOR ANIMALS", "description": "Issue: stray dogs and the ABC Rules", "decision_date": "22-08-2026",
         "path": "2026_8_1_20", "nc_display": "2026INSC900"},
        {"title": "A versus B", "description": "Arbitration award", "decision_date": "01-08-2026", "path": "2026_8_21_30"},
    ]
    items = documents.sc_items(src("judgments-sc"), rows, 2026)
    assert len(items) == 1
    it = items[0]
    assert it.date == date(2026, 8, 22) and it.title.endswith("(2026INSC900)")
    assert it.url == it.document_url == "https://indian-supreme-court-judgments.s3.ap-south-1.amazonaws.com/data/pdf/year=2026/english/2026_8_1_20_EN.pdf"
    p = ingest.build_payload(it, "summary", [])
    assert p["kind"] == "judgment" and p["court"] == "supreme-court" and p["source_type"] == "supreme-court"


def test_high_court_rows_skip_bail_and_interim():
    key = "metadata/parquet/year=2026/court=7_26/bench=dhcdb/metadata.parquet"
    rows = [
        {"title": "W.P.(C)/1/2026 of X Vs Animal Welfare Board of India", "decision_date": "2026-09-01 00:00:00", "pdf_link": "a/DLHC1_2026.pdf", "court": "Delhi High Court"},
        {"title": "BAIL APPLN/2/2026 of Y Vs State (cow slaughter)", "decision_date": "2026-09-02", "pdf_link": "b.pdf", "court": "Delhi High Court"},
        {"title": "W.P.(C)/3/2026 of Z Vs Union", "description": "stray dogs", "decision_date": "2026-09-03", "pdf_link": "c.pdf", "is_final": "False"},
    ]
    items = documents.hc_items(src("judgments-hc"), rows, key, 2026)
    assert [i.document_url for i in items] == ["https://indian-high-court-judgments.s3.ap-south-1.amazonaws.com/data/pdf/year=2026/court=7_26/bench=dhcdb/DLHC1_2026.pdf"]
    assert items[0].court == "high-court" and "Delhi High Court, 1 Sep 2026" in items[0].title


def test_high_courts_checked_once_a_day(monkeypatch):
    monkeypatch.setattr(documents, "_state", {"hc_checked": "2026-10-08T01:00:00+00:00"})
    calls = []
    out = documents.parse_judgments_hc(src("judgments-hc"), get=lambda u: calls.append(u), now=datetime(2026, 10, 8, 12, tzinfo=timezone.utc))
    assert out == [] and calls == []


def test_gazette_rows_and_filter():
    html = (
        '<span id="gvGazetteList_lbl_Ministry_0">Ministry of Fisheries, Animal Husbandry and Dairying</span>'
        '<span id="gvGazetteList_lbl_Department_0">Department of Animal Husbandry and Dairying</span>'
        '<span id="gvGazetteList_lbl_Subject_0">The Prevention of Cruelty to Animals Control and Supervision of Experiment, Breeding and Trading of Laboratory Animals Rules, 2026</span>'
        '<span id="gvGazetteList_lbl_PartSection_0">Part II-Section 3-Sub-Section (i)</span>'
        '<span id="gvGazetteList_lbl_PublishDate_0">07-Oct-2026</span>'
        '<span id="gvGazetteList_lbl_UGID_0">CG-DL-E-07102026-276913</span>'
        '<span id="gvGazetteList_lbl_Ministry_1">Ministry of Power</span>'
        '<span id="gvGazetteList_lbl_Subject_1">Notification in respect of M/s Morena I SEZ Transmission Limited.</span>'
        '<span id="gvGazetteList_lbl_PublishDate_1">07-Oct-2026</span>'
        '<span id="gvGazetteList_lbl_UGID_1">CG-DL-E-07102026-276918</span>'
    )
    rows = documents.gazette_rows(html)
    assert len(rows) == 2
    items = documents.gazette_items(src("egazette", ministries=["Ministry of Fisheries, Animal Husbandry and Dairying"]), rows,
                                    ["Ministry of Fisheries, Animal Husbandry and Dairying"])
    assert len(items) == 1
    it = items[0]
    assert it.url == "https://egazette.gov.in/WriteReadData/2026/276913.pdf" and it.date == date(2026, 10, 7)
    assert it.doc_kind == "notification" and "CG-DL-E-07102026-276913" in it.text
