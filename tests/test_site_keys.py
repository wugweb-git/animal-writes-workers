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


def test_site_keys_each_kept_separately():
    keys = summarise.site_keys(ENV, session=_Session({"gemini": ["a", "b", ""], "groq": ["g"]}))
    assert keys == {"gemini": ["a", "b"], "groq": ["g"]}


def test_site_keys_need_credentials():
    assert summarise.site_keys({}) == {}


def test_site_and_secret_keys_become_separate_backends():
    env = summarise.with_site_keys({"GEMINI_API_KEY": "secret1", "LLM_PROVIDERS": "gemini,deepseek"},
                                   {"gemini": ["site1", "secret1"], "deepseek": ["d1", "d2"]})
    backends = summarise.backends_from_env(env)
    assert len(backends) == 4  # secret1, site1 (duplicate dropped), d1, d2
