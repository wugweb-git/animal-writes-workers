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
    assert p["tags"] == ["judgment"] and p["court"] == "supreme-court" and p["source_type"] == "supreme-court"


HC_KEY = "metadata/parquet/year=2026/court=7_26/bench=dhcdb/metadata.parquet"


def test_high_court_case_types():
    skip = documents.hc_skip_reason
    for t in ("BAIL APPLN./2/2026 of Y Vs State", "CRL.M.C./5/2026 of A Vs B", "CRLMB/1/2026 of A Vs B", "CRLMP/7/2026 of A Vs B",
              "I.A./3/2026 of A Vs B", "CRLMBA/9/2026 of A Vs B", "ABA 12/2026 of A Vs B", "NA528/44/2026 of A Vs B"):
        assert skip(t) == "bail/interim/misc", t
    assert skip("WPS/4/2026 of A Vs Zoological Park") == "off-topic type"
    assert skip("MACMA/4/2026 of A Vs Insurance Co") == "off-topic type"
    assert skip("W.P.(C)/1/2026 of X Vs Union", "DISMISSED FOR DEFAULT") == "no decision"
    assert skip("W.P.(C)/1/2026 of X Vs Union") == ""
    assert skip("CRL.P/12/2026 of X Vs State") == ""  # criminal petitions: the text decides (bail ones are dropped there)
    # An animal-welfare body as a party passes the off-topic types, never the bail and interim ones.
    assert skip("WPS/4/2026 of Animal Welfare Board of India Vs A") == ""
    assert skip("BAIL APPLN./2/2026 of PETA India Vs Y") == "bail/interim/misc"


def test_high_court_candidates_since_watermark_and_url():
    rows = [
        {"title": "W.P.(C)/1/2026 of X Vs Union", "decision_date": "2026-09-01 00:00:00", "pdf_link": "court/a/DLHC1_2026-09-01.pdf", "court": "High Court of Delhi"},
        {"title": "W.P.(C)/2/2026 of X Vs Union", "decision_date": "2026-09-02", "pdf_link": "court/a/DLHC2_2026-09-02.pdf", "court": "High Court of Delhi"},
        {"title": "W.P.(C)/3/2026 of X Vs Union", "decision_date": "2026-09-02", "pdf_link": "court/a/DLHC3_2026-09-02.pdf", "court": "High Court of Delhi"},
        {"title": "W.P.(C)/4/2026 of X Vs Union", "decision_date": "2026-06-01", "pdf_link": "court/a/old.pdf"},
        {"title": "BAIL APPLN./5/2026 of Y Vs State", "decision_date": "2026-09-03", "pdf_link": "b.pdf"},
    ]
    skipped = {}
    c = documents.hc_candidates(rows, HC_KEY, date(2026, 8, 1), skipped=skipped)
    assert [x.pdf for x in c] == ["DLHC1_2026-09-01.pdf", "DLHC2_2026-09-02.pdf", "DLHC3_2026-09-02.pdf"]
    assert skipped == {"bail/interim/misc": 1}
    assert c[0].url == "https://indian-high-court-judgments.s3.ap-south-1.amazonaws.com/data/pdf/year=2026/court=7_26/bench=dhcdb/DLHC1_2026-09-01.pdf"
    mark = {"date": "2026-09-02", "pdfs": {"DLHC1_2026-09-01.pdf": "2026-09-01", "DLHC2_2026-09-02.pdf": "2026-09-02"}}
    assert [x.pdf for x in documents.hc_candidates(rows, HC_KEY, date(2026, 8, 1), mark)] == ["DLHC3_2026-09-02.pdf"]
    # Added late with an earlier date: still read within HC_GRACE days of the watermark, not before.
    late = rows + [{"title": "W.P.(C)/6/2026 of X Vs Union", "decision_date": "2026-08-25", "pdf_link": "late.pdf"},
                   {"title": "W.P.(C)/7/2026 of X Vs Union", "decision_date": "2026-08-20", "pdf_link": "too-late.pdf"}]
    assert [x.pdf for x in documents.hc_candidates(late, HC_KEY, date(2026, 8, 1), mark)] == ["late.pdf", "DLHC3_2026-09-02.pdf"]


HEADER = ("IN THE HIGH COURT OF DELHI AT NEW DELHI\nW.P.(C) 1/2026\nSHREE GOPAL GAUSHALA, Village Rampur, District Agra .....Petitioner\n"
          "Through: Mr. A, Advocate\nversus\nZOO AUTHORITY .....Respondent\nCORAM: HON'BLE MR. JUSTICE B\nJ U D G M E N T\n")


def test_high_court_text_match():
    m = documents.hc_text_match
    assert m(HEADER + "1. The petitioner was convicted under Section 11 of the Prevention of Cruelty to Animals Act, 1960.").startswith("cites")
    assert m(HEADER + "The seized cattle were carried in violation of the Karnataka Prevention of Slaughter and Preservation of Cattle Act, 2020.").startswith("cites")
    assert m(HEADER + "Offences under Sections 9 and 51 of the Wild Life (Protection) Act, 1972 are alleged.").startswith("cites")
    # Party and address lines do not count: a gaushala in the header and a zoo as respondent.
    assert m(HEADER + "1. The dispute concerns the lease of a shop.") == ""
    # A Society for the Prevention of Cruelty to Animals is a party, not the Act.
    assert m(HEADER + "The Society for the Prevention of Cruelty to Animals owns the building let to the tenant.") == ""
    assert m(HEADER + "The municipality must run the animal birth control programme and feed street dogs.").startswith("cites")
    assert m(HEADER + "Complaints about stray dogs and dog bites in the colony were ignored.").startswith("terms:")
    assert m(HEADER + "1. The dispute concerns the lease of a shop.", strong="Animal Welfare") == "party: Animal Welfare"
    assert m("", strong="Animal Welfare") == ""  # scanned: nothing to summarise from
    assert m(HEADER + "This is a petition for regular bail. FIR under the Prevention of Cruelty to Animals Act, 1960.") == ""
    assert m(HEADER + "By this application, the applicant is seeking bail in Crime No. 324 under the Maharashtra Animal Preservation Act, 1976.") == ""
    # Zoo Road is an address in Guwahati (dry run, 25 Oct 2026), not a zoo.
    assert m(HEADER + "The land near Zoo Road, Guwahati was allotted; the house on Zoo Road was sold.") == ""


class _Resp:
    def __init__(self, content=b""):
        self.content = content


def _listing(*keys):
    xml = '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><IsTruncated>false</IsTruncated>'
    for k, etag in keys:
        xml += f"<Contents><Key>{k}</Key><Size>10</Size><ETag>{etag}</ETag></Contents>"
    return xml + "</ListBucketResult>"


def test_high_courts_parse_keeps_from_text_and_moves_watermark(monkeypatch):
    monkeypatch.setattr(documents, "_state", {"hc_checked": "2026-10-08T01:00:00+00:00", "hc_etags": {"x": "y"}})
    rows = [
        {"title": f"W.P.(C)/{n}/2026 of X Vs Union", "decision_date": f"2026-10-0{n}", "pdf_link": f"p/DLHC{n}.pdf", "court": "High Court of Delhi"}
        for n in range(1, 5)
    ]
    texts = {"DLHC2.pdf": HEADER + "Offences under the Prevention of Cruelty to Animals Act, 1960 were registered."}
    monkeypatch.setattr(documents, "pdf_text", lambda content, max_pages=120: texts.get(content.decode(), HEADER + "A tenancy dispute."))
    fetched, read = [], []

    def get(url):
        return _Resp(_listing((HC_KEY, '"e1"'), (HC_KEY.replace("metadata.parquet", "metadata-mobile.parquet"), '"m"')).encode() if "list-type" in url else b"")

    def rows_for(key):
        read.append(key)
        return rows

    def fetch(url):
        fetched.append(url)
        return url.rsplit("/", 1)[1].encode()

    src_hc = src("judgments-hc")
    now = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
    out = documents.parse_judgments_hc(src_hc, get=get, now=now, rows_for=rows_for, fetch=fetch, max_pdfs=3)
    assert read == [HC_KEY]  # the mobile copy is not read
    assert len(fetched) == 3 and [i.document_url.rsplit("/", 1)[1] for i in out] == ["DLHC2.pdf"]
    it = out[0]
    assert it.court == "high-court" and it.doc_kind == "judgment" and it.date == date(2026, 10, 2)
    assert it.title == "W.P.(C)/2/2026 of X Vs Union (High Court of Delhi, 2 Oct 2026)"
    assert it.publisher == "High Court of Delhi · Indian High Court Judgments dataset (CC BY 4.0)"
    assert "Prevention of Cruelty to Animals Act" in it.text
    st = documents._state
    assert "hc_checked" not in st and "hc_etags" not in st
    assert st["hc"]["benches"][HC_KEY] == {"etag": '"e1"', "done": False, "mark": {
        "date": "2026-10-03", "pdfs": {"DLHC1.pdf": "2026-10-01", "DLHC2.pdf": "2026-10-02", "DLHC3.pdf": "2026-10-03"}}}
    p = ingest.build_payload(it, "summary", [])
    assert p["tags"] == ["judgment"] and p["court"] == "high-court" and p["source_type"] == "high-court"

    # Next run: the same file is read again (rows left), only the 4th PDF is fetched; the kept judgment is still returned.
    fetched.clear()
    out = documents.parse_judgments_hc(src_hc, get=get, now=now, rows_for=rows_for, fetch=fetch, max_pdfs=3)
    assert [u.rsplit("/", 1)[1] for u in fetched] == ["DLHC4.pdf"] and [i.url for i in out] == [it.url]
    assert st["hc"]["benches"][HC_KEY]["done"] is True

    # A download that fails (timeout, 5xx) is not marked read: tried again next run.
    st["hc"]["benches"][HC_KEY] = {"etag": '"e0"', "done": False, "mark": {"date": "2026-10-03", "pdfs": {"DLHC3.pdf": "2026-10-03"}}}

    def flaky(url):
        if url.endswith("DLHC1.pdf"):
            raise documents.requests.ConnectionError("timeout")
        return fetch(url)

    fetched.clear()
    documents.parse_judgments_hc(src_hc, get=get, now=now, rows_for=rows_for, fetch=flaky)
    assert sorted(st["hc"]["benches"][HC_KEY]["mark"]["pdfs"]) == ["DLHC2.pdf", "DLHC3.pdf", "DLHC4.pdf"]
    assert st["hc"]["benches"][HC_KEY]["done"] is False
    documents.parse_judgments_hc(src_hc, get=get, now=now, rows_for=rows_for, fetch=fetch)
    assert st["hc"]["benches"][HC_KEY]["done"] is True and fetched[-1].endswith("DLHC1.pdf")

    # Then: file unchanged and done, nothing is read.
    read.clear()
    fetched.clear()
    out = documents.parse_judgments_hc(src_hc, get=get, now=now, rows_for=rows_for, fetch=fetch)
    assert read == [] and fetched == [] and len(out) == 1


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


# Draft or final, and the number, from a Gazette notification's own text (owner, 16 Oct 2026: a draft we already held was
# imported again and summarised as rules issued).
DRAFT_TEXT = ("सा.का.वन. 864(अ).— हिंदी पाठ ... G.S.R. 864(E).—In exercise of the powers conferred by sub-section (1) of "
              "Section 17 ... hereby decided to amend the aforementioned rules; Objections and suggestions, if any, may be "
              "communicated to the Member Secretary (CCSEA) within a period of thirty days. DRAFT RULES 1. Short title.")
FINAL_TEXT = ("G.S.R. 552(E).– Whereas the draft of the Administrative Amendment Rules, 2026, was published ... inviting "
              "objections and suggestions ... And whereas the objections and suggestions received in respect of the said "
              "draft rules have been considered by the Central Government; Now, therefore, ... the Central Government "
              "hereby makes the following rules")
STANDARD_DRAFT = ("S.O. 1496(E).—The following draft of certain rules, which the Central Government proposes to make, is "
                  "hereby published for the information of all persons likely to be affected thereby; and notice is hereby "
                  "given that the said draft rules shall be taken into consideration after the expiry of thirty days.")


def test_notification_details_tells_draft_from_final():
    assert documents.notification_details(DRAFT_TEXT)[:2] == ("G.S.R. 864(E)", True)
    assert documents.notification_details(FINAL_TEXT)[:2] == ("G.S.R. 552(E)", False)
    assert documents.notification_details(STANDARD_DRAFT)[:2] == ("S.O. 1496(E)", True)
    assert documents.notification_details("no number here")[:2] == ("", False)


def test_draft_gets_title_text_and_payload(monkeypatch):
    from aw_workers import ingest
    from aw_workers.sources import Item

    class Resp:
        content = b"%PDF"
        def raise_for_status(self):
            pass

    class Session:
        def get(self, url, timeout=None):
            return Resp()

    monkeypatch.setattr(documents, "pdf_text", lambda content: DRAFT_TEXT)
    it = Item(src("egazette"), "The Prevention of Cruelty to Animals Rules, 2026", "https://egazette.gov.in/x.pdf",
              date(2026, 10, 7), "Gazette of India.", "Gazette of India", doc_kind="notification", document_url="https://egazette.gov.in/x.pdf")
    documents.add_gazette_details(it, Session())
    assert it.title.startswith("Draft ") and it.draft and it.legal_number == "G.S.R. 864(E)"
    assert "not rules in force" in it.text
    payload = ingest.build_payload(it, "A summary.")
    assert payload["legal_number"] == "G.S.R. 864(E)" and payload["draft"] is True


def test_draft_summary_always_says_draft():
    from aw_workers import summarise
    from aw_workers.sources import Item

    it = Item(src("egazette"), "Draft rules", "https://x", date(2026, 10, 7), draft=True)
    assert summarise.finalise("The ministry issued rules on lab animals.", it).startswith("Draft, open for objections:")
    assert summarise.finalise("The draft rules propose changes.", it).startswith("The draft rules")
