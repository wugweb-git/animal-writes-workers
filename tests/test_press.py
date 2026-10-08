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
    items = {"items": [
        {"link": "https://www.instagram.com/p/abc/?igshid=1", "title": "Animal Writes letters", "snippet": "", "displayLink": "www.instagram.com",
         "pagemap": {"metatags": [{"article:published_time": "2025-11-29T10:00:00Z"}]}},
        {"link": "https://example.com/old", "title": "Animal Writes", "snippet": "", "pagemap": {"metatags": [{"article:published_time": "2025-01-01"}]}},
        {"link": "https://example.com/other", "title": "Cricket scores", "snippet": ""},
    ]}
    calls = []

    def get(url, params=None, **kw):
        calls.append(params)
        return Resp(items)

    cfg = {"queries": ["Animal Writes"], "sites": ["instagram.com"], "daily_requests": 1}
    state = {}
    out = press.candidates(cfg, "k", "c", state, get=get)
    assert [o["source_url"] for o in out] == ["https://www.instagram.com/p/abc"]
    assert out[0]["platform"] == "instagram" and out[0]["source_date"] == "2025-11-29" and out[0]["type"] == "press"
    assert len(calls) == 1 and calls[0]["q"] == "Animal Writes site:instagram.com" and state["next"] == 1
    assert press.run(cfg, None, dry_run=True, env={"GOOGLE_CSE_KEY": "k", "GOOGLE_CSE_CX": "c"}, get=get, today=date(2026, 10, 8))["new"] == 1
