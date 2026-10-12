import json
from datetime import date

from aw_workers import relevance, run
from aw_workers.relevance import Candidate, Gate
from aw_workers.sources import Item, Source
from aw_workers.summarise import DryRunSummariser


class Backend:
    name, model = "fake", "m"

    def __init__(self, replies):
        self.replies, self.prompts = list(replies), []

    def ask(self, prompt):
        self.prompts.append(prompt)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class Summ:
    def __init__(self, *backends):
        self.backends, self.dead, self.used = list(backends), set(), {}

    def summarise(self, item):
        return "A dog was rescued in Pune. It is now safe."


class Log:
    def __init__(self):
        self.rejected = []

    def reject(self, entry):
        self.rejected.append(entry)


def reply(**kw):
    base = {"relevant": "yes", "category": "community-dogs", "india": "yes", "issuer": "", "reason": "dogs in Pune"}
    return json.dumps({**base, **kw})


def test_pass_and_reject_and_log():
    log = Log()
    g = Gate(Summ(Backend([reply(), reply(relevant="no", category="", reason="IPL team Royal Challengers")])), client=log)
    assert g.admit(Candidate("news", "Dog rescued in Pune", "https://a/1"))[0] == "pass"
    out, v = g.admit(Candidate("news", "Bulls beat Tigers", "https://a/2"))
    assert out == "reject" and not v.relevant
    assert log.rejected == [{"title": "Bulls beat Tigers", "source_url": "https://a/2", "reason": "IPL team Royal Challengers",
                             "type": "news", "feed": "", "model": "fake:m"}]


def test_not_india_and_unknown_category():
    # A missing/unknown category no longer rejects (the site assigns topics); India is double-checked (12 Oct 2026).
    g = Gate(Summ(Backend([reply(india="no"), reply(category="sports"), reply(india="no")])))
    assert g.admit(Candidate("news", "x", "https://a/1"))[0] == "reject"
    assert g.admit(Candidate("news", "y", "https://a/2"))[0] == "pass"
    assert g.admit(Candidate("news", "Peacock plucked by crowd in Uttar Pradesh", "https://a/3"))[0] == "pass"


def test_library_needs_known_issuer_and_counts_as_india():
    g = Gate(Summ(Backend([reply(india="no", category="courts-judgments", issuer="Supreme Court of India"),
                           reply(category="laws-rules", issuer="Some Consultancy Pvt Ltd")])),
             directory=["Blue Cross of India"])
    assert g.admit(Candidate("legal", "Judgment", "https://a/1"))[0] == "pass"
    assert g.admit(Candidate("legal", "Generic PDF", "https://a/2"))[0] == "reject"
    assert g.known_issuer("blue cross of india") and g.known_issuer("high-court") and not g.known_issuer("Acme")


def test_all_backends_fail_is_unavailable_and_cache_by_url():
    g = Gate(Summ(Backend([RuntimeError("500"), "not json"])))
    assert g.admit(Candidate("news", "x", "https://a/1"))[0] == "unavailable"
    b = Backend([reply()])
    g = Gate(Summ(b))
    assert g.admit(Candidate("news", "x", "https://a/1"))[0] == "pass"
    assert g.admit(Candidate("news", "x", "https://a/1"))[0] == "pass" and len(b.prompts) == 1


def test_dry_run_without_backends_passes_unchecked():
    assert Gate(None, dry_run=True).admit(Candidate("news", "x", "u"))[0] == "pass"
    assert Gate(None).admit(Candidate("news", "x", "u"))[0] == "unavailable"


def _item(url, title="Stray dog rescued in Pune"):
    src = Source(id="feed", name="Feed", kind="rss", type="news", url="")
    return Item(source=src, title=title, url=url, date=date(2026, 10, 9), text="A stray dog was rescued.")


def test_run_skips_rejected_and_keeps_unavailable_unseen(monkeypatch):
    items = [_item("https://n/1"), _item("https://n/2", "Dog show in Delhi"), _item("https://n/3", "Cat adopted in Goa")]
    monkeypatch.setattr(run.sources, "collect", lambda *a: items)
    posted = []

    class Client:
        def post(self, payload):
            posted.append(payload)
            return "created"

    summ = Summ(Backend([reply(), reply(relevant="no", category="", reason="entertainment"), RuntimeError("down")]))
    gate = Gate(summ, client=Log())
    seen = set()
    stats = run.run([Source(id="feed", name="Feed", kind="rss", type="news", url="")], {}, dry_run=False, summariser=summ,
                    client=Client(), fetch=lambda u: "", seen=seen, gate=gate)
    assert [p["source_url"] for p in posted] == ["https://n/1"]
    assert posted[0]["relevance"]["category"] == "community-dogs"
    assert stats.rejected == 1 and stats.unavailable == 1
    assert "https://n/2" in seen and "https://n/3" not in seen


def test_parse_tolerates_fences():
    assert relevance.parse('```json\n{"relevant": "no"}\n```') == {"relevant": "no"}
    assert relevance.parse("nope") is None


def test_existing_kinds():
    from aw_workers.relevance_existing import kind_of
    assert kind_of({"type": "post", "categories": ["research"]}) == "research"
    assert kind_of({"type": "post", "categories": ["guide"]}) == "guide"
    assert kind_of({"type": "post", "categories": []}) == "resource"
    assert kind_of({"type": "aw_legal"}) == "legal"
