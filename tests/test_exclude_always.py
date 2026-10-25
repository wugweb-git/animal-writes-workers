from aw_workers.policy import Policy


def _policy():
    kws = [{"id": i, "label": w, "variants": [], "roles": ["exclude"], "lang": "en", "searches": [], "categories": [], "sections": []}
           for i, w in enumerate(("cattle theft", "stealing cattle"))]
    return Policy.from_json({"publishers": {}, "relevance": {"enabled": False}, "keywords": kws})


def test_exclude_applies_with_filter_off():
    p = _policy()
    assert p.verdict("https://example.in/a", "feed", "Four men arrested for alleged cattle theft in Guwahati") == "filtered"
    assert p.verdict("https://example.in/b", "feed", "Two held for stealing cattle for slaughter") == "filtered"


def test_other_items_pass_with_filter_off():
    assert _policy().verdict("https://example.in/c", "feed", "Stray dogs vaccinated in Pune") == ""
