from aw_workers import focus_keywords as fk
from aw_workers import summarise


def test_clean_keeps_a_phrase_from_the_title():
    assert fk.clean('"Stray Dog Feeding Rules"', "Feeding stray dogs: what the rules say") == "stray dog feeding rules"


def test_clean_refuses_brand_unrelated_and_long():
    assert fk.clean("animal writes news", "Animal Writes news roundup") == ""
    assert fk.clean("cricket world cup", "Python rescued in Chhattisgarh") == ""
    assert fk.clean("hyderabad vet stray dogs blood", "Hyderabad vet sells blood of stray dogs") == ""
    assert fk.clean("hyderabad", "Hyderabad vet sells blood") == ""


def test_parse_reads_json_inside_prose():
    reply = 'Here you go:\n```json\n[{"id": 12, "keyword": "abc rules"}, {"id": "x", "keyword": "bad"}]\n```'
    assert fk.parse(reply) == {12: "abc rules"}
    assert fk.parse("no json") == {}


class _Backend:
    name = "fake"
    api_key = "k"

    def __init__(self, reply):
        self.reply = reply
        self.calls = 0

    def ask(self, prompt):
        self.calls += 1
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def test_ask_falls_through_to_the_next_key():
    dead = _Backend(Exception("401 unauthorized"))
    ok = _Backend('[{"id": 1, "keyword": "snow leopard survey"}]')
    owner = summarise.Summariser([dead, ok])
    assert fk.ask(owner, [{"id": 1, "title": "Snow leopards survey", "excerpt": ""}]) == {1: "snow leopard survey"}
    assert 0 in owner.dead


class _Client:
    def __init__(self):
        self.written = []

    def seo_items(self, post_type, page, per_page=100, missing_only=True):
        if post_type != "aw_news":
            return {"items": [], "pages": 1}
        return {"items": [{"id": 5, "title": "Jaipur dog shelter opens", "excerpt": "", "has_focus": False},
                          {"id": 6, "title": "Done already", "excerpt": "", "has_focus": True}], "pages": 1}

    def seo_focus(self, items):
        self.written += items
        return {"written": len(items), "skipped": []}


def test_main_writes_only_usable_keywords(monkeypatch):
    client = _Client()
    monkeypatch.setattr(fk.ingest, "Client", lambda *a, **k: client)
    monkeypatch.setattr(fk.summarise, "backends_from_env", lambda env, ledger=None: [_Backend('[{"id": 5, "keyword": "jaipur dog shelter"}, {"id": 6, "keyword": "x"}]')])
    monkeypatch.setenv("WP_BASE_URL", "https://example.test")
    monkeypatch.setenv("WP_INGEST_USER", "u")
    monkeypatch.setenv("WP_INGEST_APP_PASSWORD", "p")
    assert fk.main() == 0
    assert client.written == [{"id": 5, "keyword": "jaipur dog shelter"}]
