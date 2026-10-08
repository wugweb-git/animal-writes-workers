from datetime import date

from aw_workers import backfill, sources


def src(id_, url, type_="news"):
    return sources.Source(id=id_, name=id_, kind="rss", type=type_, url=url)


def test_month_windows_newest_first_and_floor():
    w = backfill.month_windows(date(2025, 11, 1), date(2026, 1, 15))
    assert w == [(date(2026, 1, 1), date(2026, 1, 16)), (date(2025, 12, 1), date(2026, 1, 1)), (date(2025, 11, 1), date(2025, 12, 1))]


def test_plan_windows_google_skips_opportunities():
    g = src("g", "https://news.google.com/rss/search?q=%22animal+welfare%22&hl=en-IN")
    r = src("r", "https://example.org/feed")
    o = src("o", "https://example.org/jobs", "opportunity")
    p = backfill.plan([g, r, o], date(2025, 11, 1), date(2025, 12, 10))
    ids = [s.id for s in p]
    assert ids == ["r", "g@2025-12", "g@2025-11"]
    assert "after%3A2025-12-01" in p[1].url and "before%3A2025-12-11" in p[1].url
    assert all(s.lookback_days == 39 for s in p)


def test_same_story_pairs():
    rows = [
        {"section": "News", "date": "2026-01-02", "title": "Supreme Court orders feeding spots for community dogs", "url": "a"},
        {"section": "News", "date": "2026-01-03", "title": "Supreme Court orders feeding spots for community dogs in Delhi", "url": "b"},
        {"section": "News", "date": "2026-02-03", "title": "Something else entirely about cattle", "url": "c"},
    ]
    pairs = backfill.same_story_pairs(rows)
    assert len(pairs) == 1 and pairs[0]["kept"] == "a" and pairs[0]["also"] == "b"
