from datetime import date
from pathlib import Path

from aw_workers import ingest, sources

FIX = Path(__file__).parent / "fixtures"
TODAY = date(2026, 10, 7)


def _src(sid):
    src, _ = sources.load_sources(Path(__file__).parent.parent / "sources.yaml")
    return next(s for s in src if s.id == sid)


def _collect(sid, fixture):
    return sources.collect(_src(sid), (FIX / fixture).read_text(encoding="utf-8", errors="ignore"), TODAY, 14, 15)


def test_zoho_reads_embedded_jobs_and_keeps_conservation_titles_only():
    items = _collect("opp-wwf-india", "wwf_zoho_careers.html")
    titles = [i.title for i in items]
    assert "Project Officer - Raptor Conservation Programme (M.P. & Karnataka)" in titles
    assert not any("Graphic Design" in t or "E Commerce" in t or "Fundraising" in t for t in titles)
    first = items[0]
    assert first.url.startswith("https://wwfindia.zohorecruit.com/jobs/Careers/")
    assert first.place == "Bhopal, Madhya Pradesh"


def test_sjm_reads_roles_with_place_and_unique_urls():
    items = _collect("opp-wildlife-sos", "wildlifesos_careers.html")
    assert len(items) == 10
    vet = next(i for i in items if i.title == "Veterinary Officer")
    assert vet.place
    assert vet.url.endswith("#veterinary-officer")
    assert len({i.url for i in items}) == len(items)


def test_closed_calls_are_skipped():
    # The AWBI awards invitation closed on 30.06.2026; nothing else on the page is an opportunity.
    assert _collect("opp-awbi-notifications", "awbi_notifications.html") == []


def test_deadline_in_text():
    assert sources.deadline_in("Invitation for awards. Last date to apply - 30.06.2026") == date(2026, 6, 30)
    assert sources.deadline_in("Apply by 5/11/2026") == date(2026, 11, 5)
    assert sources.deadline_in("no date here") is None


def test_opportunity_payload_and_factual_summary():
    item = _collect("opp-wildlife-sos", "wildlifesos_careers.html")[0]
    payload = ingest.build_payload(item, ingest.opportunity_summary(item))
    assert payload["type"] == "opportunity"
    assert payload["org_name"] == "Wildlife SOS"
    assert payload["opportunity_type"] == "job"
    assert payload["place"] == item.place
    assert payload["summary"].startswith("Wildlife SOS: ")
