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
    got = [(b.name, b.model, b.api_key) for b in backends]
    # each Gemini key once per free model (saved model first); "other" once; no address = skipped
    assert got[:3] == [("gemini", "gemini-flash-latest", "g1"), ("gemini", "gemini-flash-lite-latest", "g1"),
                       ("gemini", "gemini-3-flash-preview", "g1")]
    assert got[3] == ("gemini", "gemini-3-flash-preview", "g2") and len([g for g in got if g[2] == "g2"]) == 3
    assert got[-1] == ("newai", "m-1", "n1") and not any(g[2] == "n2" for g in got)


def test_secret_keys_follow_site_rows_without_repeats(monkeypatch):
    monkeypatch.setattr(summarise, "site_rows", lambda env: [{"provider": "groq", "model": "x", "url": "https://g.test", "key": "k1"}])
    backends = summarise.backends_from_env({"GROQ_API_KEY": "k1\nk2", "LLM_PROVIDERS": "groq"})
    keys = [b.api_key for b in backends]
    assert keys[-1] == "k2" and set(keys[:-1]) == {"k1"} and len(keys) == 6  # k1: saved model + 4 free Groq models


def test_openrouter_key_uses_only_free_models():
    rows = [{"provider": "openrouter", "name": "openrouter", "model": "openai/gpt-4o-mini", "url": "https://openrouter.ai/api/v1", "key": "o1"}]
    models = [b.model for b in summarise.backends_from_rows(rows)]
    assert models == summarise.EXTRA_MODELS["openrouter"]  # free-tier accounts: the paid saved model is left out
    rows[0]["model"] = "minimax/minimax-m3.1-flash:free"  # a free saved model is not repeated
    assert [b.model for b in summarise.backends_from_rows(rows)] == summarise.EXTRA_MODELS["openrouter"]


def test_cline_answer_wrapped_in_data_is_read():
    class R:
        status_code = 200

        def json(self):
            return {"data": {"choices": [{"finish_reason": "stop", "message": {"content": " ok "}}]}}

        def raise_for_status(self):
            pass

    class S:
        def post(self, *a, **kw):
            return R()

    b = summarise.OpenAICompatBackend("cline", "https://api.cline.bot/api/v1/chat/completions", "k", "m", session=S(), sleep=lambda s: None)
    assert b.ask("hi") == "ok"


def test_openrouter_keys_start_on_different_free_models():
    rows = [{"provider": "openrouter", "model": "openai/gpt-4o-mini", "url": "https://openrouter.ai/api/v1", "key": f"o{i}"} for i in range(3)]
    firsts = [next(b.model for b in summarise.backends_from_rows(rows) if b.api_key == f"o{i}") for i in range(3)]
    assert firsts == summarise.EXTRA_MODELS["openrouter"][:3]


def test_free_quota_used_drops_that_keys_free_models_only():
    class R:
        status_code, text, headers = 429, '{"error":{"message":"Rate limit exceeded: free-models-per-day"}}', {}

        def raise_for_status(self):
            raise AssertionError("not reached")

    class S:
        calls = 0

        def post(self, *a, **kw):
            S.calls += 1
            return R()

    import pytest
    with pytest.raises(summarise.FreeQuotaUsed):
        summarise._post_with_retry(S(), "u", headers={}, json={"model": "x:free"}, timeout=1, sleep=lambda s: None,
                                   min_interval=0, state={})
    assert S.calls == 1  # no retry

    rows = [{"provider": "openrouter", "model": "openai/gpt-4o-mini", "url": "https://openrouter.ai/api/v1", "key": k} for k in ("a", "b")]
    owner = summarise.Summariser(summarise.backends_from_rows(rows))
    i = next(n for n, b in enumerate(owner.backends) if b.api_key == "a" and b.model.endswith(":free"))
    summarise.note_failure(owner, i, summarise.FreeQuotaUsed("429 free-models-per-day"))
    alive = [(b.api_key, b.model) for n, b in enumerate(owner.backends) if n not in owner.dead]
    assert not any(k == "a" for k, m in alive)  # key a has only free models: all dropped for the run
    assert any(k == "b" and m.endswith(":free") for k, m in alive)
