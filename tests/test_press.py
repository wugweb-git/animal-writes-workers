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


def test_weekly_google_search(tmp_path, monkeypatch):
    monkeypatch.setattr(press, "STATE_FILE", tmp_path / "p.json")
    seen = {}

    def news(client, dry_run, cfg, since, windows):
        seen["news"] = (since, windows)
        return {"new": 1, "duplicate": 1}, []

    def web(client, dry_run, floor, periods, keys):
        seen["web"] = (floor, keys)
        return {"new": 2}, []

    out = press.run({"queries": ["Animal Writes"]}, None, dry_run=False, env={"GEMINI_API_KEY": "g"},
                    today=date(2026, 10, 20), news_search=news, web_search=web)
    assert out == {"status": "ran", "new": 3, "duplicate": 1}
    assert seen["news"][0] == date(2026, 10, 6) and seen["web"] == ("2026-10-06", ["g"])
    assert press.run({"queries": ["a"]}, None, dry_run=True, env={}, today=date(2026, 10, 22),
                     news_search=news, web_search=web)["status"] == "ran this week"


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


def test_press_web_search_never_uses_a_paid_openrouter_model():
    from aw_workers import press_gemini

    env = {"OPENROUTER_API_KEY": "o", "GEMINI_API_KEY": "g"}
    assert press_gemini.web_keys(env) == ["g"]  # no model set: the old paid default is gone
    assert press_gemini.web_keys({**env, "AW_PRESS_OPENROUTER_MODEL": "openai/gpt-4o-mini:online"}) == ["g"]
    assert press_gemini.web_keys({**env, "AW_PRESS_OPENROUTER_MODEL": "google/gemma-4-31b-it:free:online"}) == ["or:o", "g"]
