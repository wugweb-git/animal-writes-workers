from datetime import date

from aw_workers import sources
from aw_workers.submit import feed_for, parse_lines


def test_parse_lines_reads_url_date_publisher_title():
    rows = parse_lines("https://a.in/x | 2026-10-14 | Telangana Today\nnot a url\nhttps://b.in/y | 2026-10-13 | NewsBytes | A title")
    assert [r["url"] for r in rows] == ["https://a.in/x", "https://b.in/y"]
    assert rows[0]["date"] == date(2026, 10, 14) and rows[0]["title"] == ""
    assert rows[1]["publisher"] == "NewsBytes" and rows[1]["title"] == "A title"


def test_feed_parses_back_to_one_item_with_its_date_and_publisher():
    row = {"url": "https://a.in/x?a=1&b=2", "date": date(2026, 10, 14), "publisher": "Telangana Today", "title": ""}
    src = sources.Source(id="s", name="Telangana Today", kind="rss", type="news", url="submit://0", lookback_days=100_000)
    items = sources.parse_rss(src, feed_for(row, "Rats & monkeys", "Draft rules on animals"))
    assert len(items) == 1
    assert items[0].url == row["url"] and items[0].date == date(2026, 10, 14)
    assert items[0].title == "Rats & monkeys" and items[0].publisher == "Telangana Today"
