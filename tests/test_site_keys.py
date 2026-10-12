from aw_workers import summarise


class _Resp:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


class _Session:
    def __init__(self, data):
        self.data = data

    def get(self, url, **kw):
        assert url.endswith("/wp-json/aw/v1/ai-keys")
        return _Resp(self.data)


ENV = {"WP_BASE_URL": "https://example.test", "WP_INGEST_USER": "u", "WP_INGEST_APP_PASSWORD": "p"}


def test_site_rows_need_credentials():
    assert summarise.site_rows({}) == []


def test_site_rows_skip_empty_keys():
    rows = summarise.site_rows(ENV, session=_Session({"rows": [{"provider": "gemini", "key": "a"}, {"provider": "groq", "key": " "}]}))
    assert [r["key"] for r in rows] == ["a"]


def test_each_row_is_its_own_backend_in_order():
    rows = [
        {"provider": "gemini", "model": "gemini-flash-latest", "key": "g1"},
        {"provider": "gemini", "model": "gemini-3-flash-preview", "key": "g2"},
        {"provider": "other", "name": "newai", "model": "m-1", "url": "https://x.test/v1/chat/completions", "key": "n1"},
        {"provider": "other", "name": "nourl", "model": "m-1", "url": "", "key": "n2"},  # skipped: no address
    ]
    backends = summarise.backends_from_rows(rows)
    assert [(b.name, b.model, b.api_key) for b in backends] == [
        ("gemini", "gemini-flash-latest", "g1"), ("gemini", "gemini-3-flash-preview", "g2"), ("newai", "m-1", "n1")]


def test_secret_keys_follow_site_rows_without_repeats(monkeypatch):
    monkeypatch.setattr(summarise, "site_rows", lambda env: [{"provider": "groq", "model": "x", "url": "https://g.test", "key": "k1"}])
    backends = summarise.backends_from_env({"GROQ_API_KEY": "k1\nk2", "LLM_PROVIDERS": "groq"})
    assert [b.api_key for b in backends] == ["k1", "k2"]
