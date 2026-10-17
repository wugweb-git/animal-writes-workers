"""Campaign 2.0 (draft lab animal rules 2026): CCSEA What's New rows, topic tags and the final-rules hold."""

from datetime import date
from pathlib import Path

import yaml

from aw_workers import sources as S
from aw_workers.ingest import build_payload

ROWS = """<table><tr><th>S.No.</th><th>What's New</th><th>File</th></tr>
<tr><td>1</td><td> Draft Prevention of Cruelty to Animals (Laboratory Animals) Rules, 2026 for public comments. </td>
<td><a href="/WriteReadData/LnPdf/Draft.pdf"><img></a></td></tr>
<tr><td>2</td><td>Vaccine trials using Dogs</td><td><a href="/WriteReadData/LnPdf/Vaccine.pdf"><img></a></td></tr>
</table>"""


def _lists():
    return yaml.safe_load(Path(__file__).resolve().parents[1].joinpath("sources.yaml").read_text(encoding="utf-8"))


def test_ccsea_rows_and_baseline():
    src = S.Source("ccsea", "CCSEA", "ccsea", "legal", "https://ccsea.gov.in/CCSEAWhatsNews.aspx",
                   skip_urls=["https://ccsea.gov.in/WriteReadData/LnPdf/Draft.pdf"])
    items = S.parse_ccsea(src, ROWS)
    assert [i.title for i in items] == ["Vaccine trials using Dogs"]
    assert items[0].doc_kind == "circular" and items[0].document_url.endswith("Vaccine.pdf")
    kept = S.collect(src, ROWS, date(2026, 10, 16), 30, 15)
    assert kept[0].date == date(2026, 10, 16)  # no date on the page: the day we found it


def test_rules_row_is_a_rule():
    src = S.Source("ccsea", "CCSEA", "ccsea", "legal", "https://ccsea.gov.in/CCSEAWhatsNews.aspx")
    assert S.parse_ccsea(src, ROWS)[0].doc_kind == "rule"


def test_topic_tag_and_hold():
    S.load_sources_data(_lists())
    assert S.topic_tags_for("CCSEA draft Laboratory Animals Rules, 2026: objections till 5 Nov") == (["campaign-lab-animals-2026"], "")
    tags, hold = S.topic_tags_for("Breeding and Trading of Laboratory Animals) Rules, 2026 notified in the Gazette")
    assert tags == ["campaign-lab-animals-2026"] and hold.startswith("campaign-lab-animals-2026: final")
    assert S.topic_tags_for("Stray dog feeding rules in Delhi") == ([], "")


def test_payload_carries_tag_and_hold():
    S.load_sources_data(_lists())
    src = S.Source("gnews-lab-animal-rules", "Google News", "rss", "news", "https://news.google.com/rss")
    item = S.Item(src, "CCSEA notifies Laboratory Animals Rules, 2026", "https://example.in/a", date(2026, 11, 20))
    p = build_payload(item, "The final rules were published in the Gazette.")
    assert "campaign-lab-animals-2026" in p["tags"] and p["hold"]
    S.set_sections({"aw_news": {"tags": ["research"]}})  # a section that does not allow the tag drops it
    try:
        assert "tags" not in build_payload(item, "Final rules.")
    finally:
        S.SECTIONS.clear()


def test_new_sources_load():
    ids = {s.id for s in S.load_sources_data(_lists())[0]}
    assert {"ccsea-whats-new", "gnews-lab-animal-rules", "pfa-substack", "peta-india"} <= ids


def test_lab_rules_coverage_goes_to_media_and_pr():
    srcs, _ = S.load_sources_data(_lists())
    src = next(s for s in srcs if s.id == "gnews-lab-animal-rules")
    assert src.file_as == "press"
    item = S.Item(src, "Centre proposes CCSEA draft rules on animal experiments", "https://example.com/story", date(2026, 10, 14),
                  publisher="Example Times", image="https://example.com/a.jpg")
    p = build_payload(item, "")
    assert p["type"] == "press" and p["source_name"] == "Example Times" and p["image_url"] == "https://example.com/a.jpg"
    assert p["tags"] == ["campaign-lab-animals-2026"] and p["source_date"] == "2026-10-14" and p["feed"] == src.id
    assert "summary" not in p and "categories" not in p
