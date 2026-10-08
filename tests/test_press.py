from datetime import date

from aw_workers import press


class Resp:
    def __init__(self, data, status=200):
        self._d, self.status_code, self.ok = data, status, status == 200

    def json(self):
        return self._d

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


def test_normalise_and_platform():
    assert press.normalise("http://m.facebook.com/AnimalWritesIndia/posts/1/?fbclid=x") == "https://www.facebook.com/AnimalWritesIndia/posts/1"
    assert press.normalise("https://twitter.com/AnimalWrites/status/5") == "https://x.com/AnimalWrites/status/5"
    assert press.platform_of("https://www.instagram.com/p/abc") == "instagram"
    assert press.platform_of("https://www.thehindu.com/x") == "web"


def test_disabled_without_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(press, "STATE_FILE", tmp_path / "p.json")
    assert press.run({"queries": ["Animal Writes"]}, None, dry_run=True, env={})["status"] == "disabled: no credentials"


def test_candidates_relevance_floor_and_rotation(tmp_path, monkeypatch):
    monkeypatch.setattr(press, "STATE_FILE", tmp_path / "p.json")
    items = {"web": {"results": [
        {"url": "https://www.instagram.com/p/abc/?igshid=1", "title": "<strong>Animal Writes</strong> letters", "description": "", "page_age": "2025-11-29T10:00:00"},
        {"url": "https://example.com/old", "title": "Animal Writes", "description": "", "page_age": "2025-01-01T00:00:00"},
        {"url": "https://example.com/other", "title": "Cricket scores", "description": ""},
    ]}}
    calls = []

    def get(url, params=None, **kw):
        calls.append(params)
        return Resp(items)

    cfg = {"queries": ["Animal Writes"], "sites": ["instagram.com"], "daily_requests": 1}
    state = {}
    out = press.candidates(cfg, "k", state, get=get, pause=0)
    assert [o["source_url"] for o in out] == ["https://www.instagram.com/p/abc"]
    assert out[0]["platform"] == "instagram" and out[0]["source_date"] == "2025-11-29" and out[0]["type"] == "press"
    assert len(calls) == 1 and calls[0]["q"] == "Animal Writes site:instagram.com" and state["next"] == 1
    cfg["pause_seconds"] = 0
    assert press.run(cfg, None, dry_run=True, env={"BRAVE_SEARCH_KEY": "k"}, get=get, today=date(2026, 10, 8))["new"] == 1


def test_formats_and_handles():
    assert press.format_of("https://www.instagram.com/reel/abc/", "instagram") == "Instagram reel"
    assert press.format_of("https://www.instagram.com/p/abc/", "instagram") == "Instagram post"
    assert press.format_of("https://www.facebook.com/x/videos/1", "facebook") == "Facebook video"
    assert press.format_of("https://x.com/a/status/1", "x") == "X post"
    assert press.format_of("https://www.reddit.com/r/india/comments/1/x", "reddit") == "Reddit thread"
    assert press.format_of("https://www.thehindu.com/news/x", "web") == "Article"
    assert press.handle_of("https://x.com/AnimalWrites/status/5", "x") == "@AnimalWrites"
    assert press.handle_of("https://www.reddit.com/r/india/comments/1/x", "reddit") == "r/india"
    assert press.handle_of("https://www.instagram.com/p/abc/", "instagram") == ""


def test_weekly_and_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(press, "STATE_FILE", tmp_path / "p.json")
    press.save_state({"last_run": "2026-10-05", "month": "2026-10", "used_month": 10, "backfilled": True})
    env = {"BRAVE_SEARCH_KEY": "k"}
    assert press.run({"queries": ["a"]}, None, dry_run=True, env=env, today=date(2026, 10, 8))["status"] == "ran this week"
    press.save_state({"last_run": "2026-09-01", "month": "2026-10", "used_month": 900, "backfilled": True})
    assert press.run({"queries": ["a"]}, None, dry_run=True, env=env, today=date(2026, 10, 8))["status"] == "budget used"
