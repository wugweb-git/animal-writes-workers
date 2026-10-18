from aw_workers import focus_keywords as fk
from aw_workers import summarise


def test_clean_keeps_a_phrase_from_the_title():
    assert fk.clean('"Stray Dog Feeding Rules"', "Feeding stray dogs: what the rules say") == "stray dog feeding rules"


def test_clean_refuses_brand_unrelated_and_long():
    assert fk.clean("animal writes news", "Weekly news roundup") == ""  # site name, title not about the site
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


def test_clean_rules_shared_with_the_site():
    assert fk.clean("india writes for", "India Writes for Animal Rights") == ""          # ends on a connector
    assert fk.clean("save animals", "Save animals now") == ""                           # only generic words
    assert fk.clean("animal writes campaign", "Animal Writes 1.0") == "animal writes campaign"  # title names the site
    assert fk.clean("#BeTheirVoice stray dogs", "#BeTheirVoice #AnimalWrites") == "betheirvoice stray dogs"  # no hashtags
    # a word from the item's own text is enough when the title shares none
    assert fk.clean("stray dog protest", "Heartbroken, enraged", "Residents protest after stray dog killed") == "stray dog protest"
    assert fk.why_refused("rescued in", "Python rescued in Chhattisgarh") == 'ends on "in"'


def test_main_asks_again_for_refused_phrases(monkeypatch):
    client = _Client()
    backend = _Backend('[{"id": 5, "keyword": "animal welfare"}]')
    replies = iter(['[{"id": 5, "keyword": "animal welfare"}]', '[{"id": 5, "keyword": "jaipur dog shelter"}]'])
    backend.ask = lambda prompt: next(replies)
    monkeypatch.setattr(fk.ingest, "Client", lambda *a, **k: client)
    monkeypatch.setattr(fk.summarise, "backends_from_env", lambda env, ledger=None: [backend])
    monkeypatch.setenv("WP_BASE_URL", "https://example.test")
    monkeypatch.setenv("WP_INGEST_USER", "u")
    monkeypatch.setenv("WP_INGEST_APP_PASSWORD", "p")
    assert fk.main() == 0
    assert client.written == [{"id": 5, "keyword": "jaipur dog shelter"}]


# The SEO title is the source of truth (owner, 15 Oct 2026): the keyword is words that stand together in it.

def test_keyword_must_stand_together_in_the_seo_title():
    seo = "Pune bans fodder transport outside district amid drought"
    assert fk.clean("pune fodder transport ban", "Pune bans fodder", "", seo) == ""  # words reordered and changed
    assert fk.why_refused("pune fodder ban", "Pune bans fodder", "", seo) == "is not words that stand together in the seo_title"
    assert fk.clean("fodder transport", "Pune bans fodder", "", seo) == "fodder transport"
    assert fk.clean("Pune bans fodder transport", "Pune bans fodder", "", seo) == "pune bans fodder transport"


def test_in_title_reads_words_as_rank_math_does():
    assert fk.in_title("india's declining birdlife", "India’s declining birdlife affects more than biodiversity")
    assert fk.in_title("human-wildlife conflict", "Coimbatore human-wildlife conflict centre – Animal Writes")
    # Rank Math keeps a hyphen or apostrophe inside a word: these fail its "keyword in title" test (36 points)
    assert not fk.in_title("human wildlife conflict", "Coimbatore human-wildlife conflict centre – Animal Writes")
    assert not fk.in_title("indias declining birdlife", "India’s declining birdlife affects more than biodiversity")
    assert not fk.in_title("animal writes", "Strangulation suspected in puppy’s death – Animal Writes")
    assert not fk.in_title("dog bite", "Dog bites rise")  # whole words only


def test_clean_has_no_hyphens_and_keeps_inner_apostrophes():
    seo = "Why India Must Champion Animal Free Drug Testing"  # titles have no hyphens (owner, 18 Oct 2026)
    assert fk.clean("“Animal-free drug testing”", seo, "", seo) == "animal free drug testing"
    assert fk.clean("gaurav taneja’s dog", "Gaurav Taneja’s Dog Mau Dies", "", "Gaurav Taneja’s Dog Mau Dies") == "gaurav taneja’s dog"
    assert fk.from_title("Human Elephant Conflict a Major Challenge").startswith("human elephant")


def test_from_title_picks_specific_words_of_the_seo_title():
    kw = fk.from_title("Dehradun Zoo changes diets of animals, birds to protect them")
    assert kw and fk.in_title(kw, "Dehradun Zoo changes diets of animals, birds to protect them")
    assert kw.split()[0] not in fk.DANGLING and kw.split()[-1] not in fk.DANGLING
    assert fk.from_title("Save animals – Animal Writes") == ""  # only generic words: nothing usable


class _RepickClient(_Client):
    def __init__(self):
        super().__init__()
        self.replace = None
        self.missing = None

    def seo_items(self, post_type, page, per_page=100, missing_only=True):
        self.missing = missing_only
        if post_type != "aw_news":
            return {"items": [], "pages": 1}
        return {"items": [
            {"id": 7, "title": "Pune bans fodder transport", "seo_title": "Pune bans fodder transport outside district amid drought",
             "excerpt": "", "has_focus": True, "focus": "pune fodder transport ban", "in_title": False},
            {"id": 8, "title": "Already fine", "seo_title": "Jaipur dog shelter opens", "excerpt": "", "has_focus": True,
             "focus": "jaipur dog shelter", "in_title": True},
        ], "pages": 1}

    def seo_focus(self, items, replace=False):
        self.replace = replace
        return super().seo_focus(items)


def _env(monkeypatch, client, reply):
    monkeypatch.setattr(fk.ingest, "Client", lambda *a, **k: client)
    monkeypatch.setattr(fk.summarise, "backends_from_env", lambda env, ledger=None: [_Backend(reply)])
    monkeypatch.setenv("WP_BASE_URL", "https://example.test")
    monkeypatch.setenv("WP_INGEST_USER", "u")
    monkeypatch.setenv("WP_INGEST_APP_PASSWORD", "p")
    monkeypatch.setenv("AW_SEO_TYPES", "aw_news")


def test_repick_replaces_only_keywords_not_in_the_seo_title(monkeypatch):
    client = _RepickClient()
    _env(monkeypatch, client, '[{"id": 7, "keyword": "pune bans fodder transport"}]')
    monkeypatch.setenv("AW_SEO_REPICK", "1")
    assert fk.main() == 0
    assert client.missing is False and client.replace is True
    assert client.written == [{"id": 7, "keyword": "pune bans fodder transport"}]


def test_falls_back_to_words_of_the_seo_title_when_the_ai_phrase_is_not_in_it(monkeypatch):
    client = _RepickClient()
    _env(monkeypatch, client, '[{"id": 7, "keyword": "pune fodder transport ban"}]')  # refused twice
    monkeypatch.setenv("AW_SEO_REPICK", "1")
    assert fk.main() == 0
    assert len(client.written) == 1 and client.written[0]["id"] == 7
    assert fk.in_title(client.written[0]["keyword"], "Pune bans fodder transport outside district amid drought")


def test_from_title_prefers_a_run_without_connectors():
    kw = fk.from_title("SC’s Order on Street Dogs: Opportunities and Implementation")
    assert kw and not set(kw.split()) & fk.DANGLING  # never "order on street"
