from aw_workers import ai_usage, summarise


class Fake(summarise.GroqBackend):
    def __init__(self, key, replies):
        self.api_key, self.model, self.name, self.replies = key, "openai/gpt-oss-120b", "groq", list(replies)


def _ask(self, prompt):
    r = self.replies.pop(0)
    if isinstance(r, Exception):
        raise r
    return r


def setup_function():
    ai_usage.USAGE.clear()


def test_calls_are_counted_per_key_and_model(monkeypatch):
    monkeypatch.setattr(Fake, "ask", ai_usage.counted(_ask), raising=False)
    b = Fake("gsk_secretKEY1", ["fine", RuntimeError("429 Too Many Requests"), RuntimeError("500 boom")])
    for _ in range(3):
        try:
            b.ask("x")
        except RuntimeError:
            pass
    row = ai_usage.USAGE[ai_usage.key_id("gsk_secretKEY1")]
    assert (row["calls"], row["ok"], row["limited"], row["failed"]) == (3, 1, 1, 1)
    assert row["last4"] == "KEY1" and row["provider"] == "groq"
    assert row["models"]["openai/gpt-oss-120b"]["calls"] == 3
    assert "gsk_secretKEY1" not in repr(ai_usage.USAGE)


def test_key_id_matches_the_site():
    # PHP: substr( hash( 'sha256', $key ), 0, 16 )
    assert ai_usage.key_id("abc") == "ba7816bf8f01cfea"


def test_report_sends_counts_without_the_key():
    ai_usage.USAGE["ba7816bf8f01cfea"] = {"provider": "gemini", "last4": "zzzz", "calls": 2, "ok": 2, "limited": 0, "failed": 0, "models": {}}
    sent = {}

    class S:
        def post(self, url, json, auth, timeout):
            sent.update(url=url, body=json)

            class R:
                def raise_for_status(self):
                    pass

            return R()

    env = {"WP_BASE_URL": "https://animalwrites.in", "WP_INGEST_USER": "ingest", "WP_INGEST_APP_PASSWORD": "pw",
           "GITHUB_WORKFLOW": "Sourcing workers", "GITHUB_RUN_ID": "1", "GITHUB_REPOSITORY": "o/r"}
    assert ai_usage.report(env, session=S())
    assert sent["url"].endswith("/wp-json/aw/v1/ai-usage")
    assert sent["body"]["workflow"] == "Sourcing workers" and sent["body"]["keys"][0]["id"] == "ba7816bf8f01cfea"
    assert ai_usage.USAGE == {}


def test_real_backends_are_wrapped():
    for cls in (summarise.ClaudeBackend, summarise.GeminiBackend, summarise.GroqBackend):
        assert hasattr(cls.__dict__["ask"], "__wrapped__")
