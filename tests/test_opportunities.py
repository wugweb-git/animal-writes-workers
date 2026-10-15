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


WSOS = sources.Source("wsos", "Wildlife SOS careers", "sjm", "opportunity", "https://wildlifesos.org/about-us/careers/", org="Wildlife SOS", opportunity_type="job")


def test_sjm_reads_roles_with_place_and_unique_urls():
    # Fetched by the site in production (Cloudflare blocks GitHub); the reader stays tested here.
    items = sources.collect(WSOS, (FIX / "wildlifesos_careers.html").read_text(encoding="utf-8", errors="ignore"), TODAY, 14, 15)
    assert len(items) == 11
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
    item = _collect("opp-wwf-india", "wwf_zoho_careers.html")[0]
    payload = ingest.build_payload(item, ingest.opportunity_summary(item))
    assert payload["type"] == "opportunity"
    assert payload["org_name"] == "WWF-India"
    assert payload["tags"] == ["job"]
    assert payload["place"] == item.place
    assert payload["summary"].startswith("WWF-India: ")


def test_deadline_in_written_month():
    from aw_workers.sources import deadline_in
    from datetime import date
    assert deadline_in("Last date for application - 5 September 2026 WWF-India is an equal") == date(2026, 9, 5)
    assert deadline_in("Apply by 2nd Oct, 2026") == date(2026, 10, 2)
    assert deadline_in("Last date to apply - 31.10.2026") == date(2026, 10, 31)
    assert deadline_in("no date here") is None
