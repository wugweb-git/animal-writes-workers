"""The shared keyword matcher (sourcing restructure phase 1, owner 25 Oct 2026): word mode against the shared fixture
(the site runs the same file), and the legacy behaviour kept until the site sends `match: "word"`."""

import json
import re
from datetime import date
from pathlib import Path

import pytest

from aw_workers import matcher, policy, sources

CASES = json.loads((Path(__file__).parent / "fixtures" / "match_cases.json").read_text(encoding="utf-8"))["cases"]


@pytest.fixture(autouse=True)
def _legacy_after():
    yield
    matcher.set_mode(None)


@pytest.mark.parametrize("case", CASES, ids=[f"{c['term']!r} in {c['text'][:30]!r}" for c in CASES])
def test_word_mode_matches_the_shared_fixture(case):
    assert matcher.matches(case["text"], case["term"]) is case["word"], case["note"]


def test_policy_has_term_agrees_with_the_matcher_on_plain_latin_terms():
    """policy.has_term (the site's aw_has_term twin) already matches whole words; the new matcher must not differ."""
    checked = 0
    for c in CASES:
        t = c["term"].strip().lower()
        if t and "  " not in t and re.fullmatch(r"[a-z0-9 \-'’]+", t) and "  " not in c["text"] and "\t" not in c["text"]:
            assert policy.has_term(c["text"].lower(), t) is c["word"], c
            checked += 1
    assert checked >= 15


def test_mode_switch_is_only_word_or_legacy():
    assert matcher.mode() == "legacy"
    matcher.set_mode("word")
    assert matcher.word_mode()
    for v in (None, "", "substring", "WORDS", 1):
        matcher.set_mode(v)
        assert matcher.mode() == "legacy"
    matcher.set_mode(" Word ")
    assert matcher.word_mode()


def _item(title, text="", match="text"):
    src = sources.Source(id="s", name="S", kind="rss", type="news", url="https://example.com/feed", match=match)
    return sources.Item(source=src, title=title, url="https://example.com/1", date=date(2026, 10, 25), text=text)


def test_legacy_mode_keeps_the_old_substring_checks():
    """Until the site sends match: "word", per-source keyword checks, category words and topic tags match as before."""
    sources.load_sources_data({"sources": [], "categories": {"pets-breeding": ["pet"], "cruelty-prosecution": ["sterilis"]},
                               "topic_tags": {"lab": {"any": ["lab animal"]}}})
    assert matcher.mode() == "legacy"
    assert sources.matches_keywords(_item("Dogma and petrol"), ["dog"])  # substring, as before
    assert sources.categories_for(_item("Sterilisation drive")) == ["cruelty-prosecution"]  # >4 letters: substring
    assert sources.categories_for(_item("Petrol prices")) == []  # <=4 letters: whole word, as before
    assert sources.topic_tags_for("New lab animals rules")[0] == ["lab"]


def test_word_mode_from_the_lists_switches_every_check():
    sources.load_sources_data({"match": "word", "sources": [],
                               "categories": {"pets-breeding": ["pet"], "cruelty-prosecution": ["sterilis", "poach*"]},
                               "topic_tags": {"lab": {"any": ["lab animal"], "hold": ["rules"], "unless": ["draft"]}}})
    assert matcher.word_mode()
    assert not sources.matches_keywords(_item("Dogma and petrol"), ["dog"])
    assert sources.matches_keywords(_item("Stray dogs fed"), ["dog"])
    assert sources.matches_keywords(_item("x", "two dogs", match="text"), ["dog"])
    assert not sources.matches_keywords(_item("x", "two dogs", match="title"), ["dog"])
    assert sources.categories_for(_item("Sterilisation drive")) == []  # whole word now: needs "sterilis*"
    assert sources.categories_for(_item("Pets and poaching")) == ["pets-breeding", "cruelty-prosecution"]
    tags, hold = sources.topic_tags_for("New lab animals rules")
    assert tags == ["lab"] and hold.startswith("lab:")
    assert sources.topic_tags_for("New lab animals rules, a draft")[1] == ""  # unless word
    sources.load_sources_data({"sources": []})  # lists without `match` go back to legacy
    assert matcher.mode() == "legacy"


def test_policy_has_term_understands_the_prefix_star():
    assert policy.has_term("a sterilization drive", "steriliz*")
    assert not policy.has_term("desterilization", "steriliz*")
    assert policy.has_term("stray dogs", "dog")  # unchanged for plain terms
