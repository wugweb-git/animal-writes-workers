from aw_workers import run as run_mod
from aw_workers.policy import Policy, host

POLICY = {
    "publishers": {"blocked": ["bollyspice.com"], "allowed": ["thehindu.com"], "gnews_allow_only": False},
    "relevance": {
        "enabled": True,
        "strong": ["animal welfare", "रेबीज"],
        "animal": ["dog", "cow"],
        "context": ["court", "rescue"],
        "exclude": ["bull market"],
    },
}


def test_host_normalises_www():
    assert host("https://www.BollySpice.com/x") == "bollyspice.com"


def test_blocked_publisher():
    p = Policy.from_json(POLICY)
    assert p.verdict("https://www.bollyspice.com/a", "gnews-animal-welfare", "animal welfare") == "blocked"


def test_relevance_rules():
    p = Policy.from_json(POLICY)
    assert p.verdict("https://example.com/a", "src", "New animal welfare board") == ""
    assert p.verdict("https://example.com/a", "src", "Dog rescue in Pune") == ""
    assert p.verdict("https://example.com/a", "src", "Dog wins a show") == "filtered"
    assert p.verdict("https://example.com/a", "src", "Bull market for cow futures, court says") == "filtered"
    assert p.verdict("https://example.com/a", "src", "रेबीज के मामले बढ़े") == ""


def test_allowed_publisher_skips_keyword_check():
    p = Policy.from_json(POLICY)
    assert p.verdict("https://thehindu.com/a", "src", "Budget day") == ""


def test_aggregator_allow_only_switch():
    data = {**POLICY, "publishers": {**POLICY["publishers"], "gnews_allow_only": True}}
    p = Policy.from_json(data)
    assert p.verdict("https://example.com/a", "gnews-animal-welfare", "animal welfare") == "blocked"
    assert p.verdict("https://example.com/a", "applenews-x", "animal welfare") == "blocked"
    assert p.verdict("https://example.com/a", "livelaw-top", "animal welfare") == ""


def test_run_skips_blocked_before_summarising(fixture_text):
    from tests.test_run import all_sources, fake_fetch

    class Never:
        used = {}

        def summarise(self, item):
            raise AssertionError("blocked items must not be summarised")

    srcs, defaults = all_sources()
    block_all = Policy(blocked={host(i) for i in ["https://news.google.com"]})
    block_all.verdict = lambda url, feed, text: "blocked"
    stats = run_mod.run(srcs, defaults, dry_run=True, summariser=Never(), client=None,
                        fetch=fake_fetch(fixture_text), policy=block_all)
    assert stats.skipped > 0 and stats.skipped + stats.duplicate == stats.fetched and stats.failed == 0 and stats.new == 0
