"""Sourcing restructure phase 1 (owner, 25 Oct 2026): the site sends `sources_v2` (each source with its publisher host,
relay switch, "read by" marker and keyword check) beside the unchanged v1 lists. Workers read v2 when it is there and
v1 otherwise, and must build exactly the same sources and searches from either."""

import copy
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml

from aw_workers import relay, relevance, run, sources

ROOT = Path(__file__).resolve().parent.parent
YAML = yaml.safe_load((ROOT / "sources.yaml").read_text(encoding="utf-8"))
CODE_KINDS = {"awbi", "zoho", "sjm", "ccsea", "egazette", "judgments-sc", "judgments-hc"}


def as_v2(config: dict, relay_hosts=()) -> dict:
    """What the site's policy sends from phase 1: the v1 lists unchanged plus sources_v2 built from the same sources."""
    out = copy.deepcopy(config)
    v2 = []
    for s in config["sources"]:
        host = (urlsplit(s["url"]).hostname or "").lower().removeprefix("www.")
        v2.append({**copy.deepcopy(s), "host": host, "relay": any(host == h or host.endswith("." + h) for h in relay_hosts),
                   "read_by": "code" if s["kind"] in CODE_KINDS else "feed", "check": {"roles": [], "categories": []}})
    out["schema"] = {"sources": 2}
    out["sources_v2"] = v2
    return out


def fields(src_list):
    return [vars(s) for s in src_list]


def test_v1_and_v2_build_the_same_sources():
    v1, d1 = sources.load_sources_data(copy.deepcopy(YAML))
    v2, d2 = sources.load_sources_data(as_v2(YAML))
    assert fields(v1) == fields(v2) and d1 == d2
    assert len(v2) == len(YAML["sources"])


def test_identical_searches_from_v1_and_v2():
    """Acceptance test (plan §6): the (feed id, query URL) list from the v1 data equals the one from the v2 data."""
    v1 = run.keyword_searches(sources.load_sources_data(copy.deepcopy(YAML))[0], YAML)
    lists = as_v2(YAML)
    v2 = run.keyword_searches(sources.load_sources_data(lists)[0], lists)
    assert [(s.id, s.url) for s in v1] == [(s.id, s.url) for s in v2]
    assert any(s.id.startswith("gnews-kw-") for s in v2)


def test_a_switched_off_source_stays_off_in_v2():
    lists = as_v2(YAML)
    lists["sources_v2"][0]["on"] = False
    got = [s.id for s in sources.load_sources_data(lists)[0]]
    assert lists["sources_v2"][0]["id"] not in got and len(got) == len(YAML["sources"]) - 1


def test_keyword_check_filter_switch_still_applies_in_v2():
    lists = as_v2({"keywords": ["stray dog"], "sources": [{"id": "a", "name": "A", "kind": "rss", "type": "news",
                                                             "url": "https://a.example/feed", "filter": True}]})
    assert sources.load_sources_data(lists)[0][0].keywords == ["stray dog"]


def test_keyword_check_by_role_and_category():
    lists = as_v2({"keywords": ["seed"], "categories": {"animal-experimentation": ["vivisection", "lab animal"]},
                   "sources": [{"id": "a", "name": "A", "kind": "rss", "type": "news", "url": "https://a.example/feed",
                                "filter": True, "keywords": ["old"]}]})
    lists["sources_v2"][0]["check"] = {"roles": ["include"], "categories": ["animal-experimentation"]}
    got = sources.load_sources_data(lists, relevance={"strong": ["animal welfare", "vivisection"]})[0][0]
    assert got.keywords == ["animal welfare", "vivisection", "lab animal"]
    # without the relevance lists (sources.yaml fallback) Include reads the lists' keywords
    assert sources.load_sources_data(lists)[0][0].keywords == ["seed", "vivisection", "lab animal"]
    lists["sources_v2"][0]["check"] = {"roles": ["include"], "categories": []}
    assert sources.load_sources_data(lists)[0][0].keywords == ["seed"]  # filter: True -> the v1 filter words


def test_incomplete_v2_falls_back_to_v1():
    lists = as_v2(YAML)
    del lists["sources_v2"][3]["url"]
    v1 = sources.load_sources_data(copy.deepcopy(YAML))[0]
    assert fields(sources.load_sources_data(lists)[0]) == fields(v1)
    lists["sources_v2"] = "not a list"
    assert fields(sources.load_sources_data(lists)[0]) == fields(v1)


def test_relay_hosts_from_v1_list_and_v2_switches():
    v1_hosts = ["zoomtventertainment.com", "awbi.gov.in", "egazette.gov.in", "ccsea.gov.in", "peopleforanimals.substack.com"]
    lists = as_v2(YAML, relay_hosts=v1_hosts)
    hosts = sources.relay_hosts(lists, v1_hosts)
    assert hosts[: len(v1_hosts)] == v1_hosts and set(hosts) == set(v1_hosts)
    # no v1 list (or an older site): the v2 switches alone still cover the PFA Substack feed
    only_v2 = sources.relay_hosts(lists, [])
    assert "peopleforanimals.substack.com" in only_v2 and "awbi.gov.in" in only_v2
    assert sources.relay_hosts(copy.deepcopy(YAML), None) == []


def test_pfa_substack_still_relayed_with_the_narrower_host():
    """Owner (25 Oct): relay only peopleforanimals.substack.com, not every substack.com subdomain."""

    class Client:
        session = None
        url = "https://animalwrites.in/wp-json/aw/v1/ingest"

    relay.configure(Client(), sources.relay_hosts(as_v2(YAML), ["peopleforanimals.substack.com"]))
    assert relay.allowed("https://peopleforanimals.substack.com/feed")
    assert not relay.allowed("https://someoneelse.substack.com/feed")
    assert not relay.allowed("https://substack.com/feed")


def test_v2_blocks_beside_config_reach_the_lists():
    raw = {"config": {"sources": [1]}, "schema": {"sources": 2}, "sources_v2": [{"id": "x"}], "match": "word",
           "category_names": {"a": "A"}, "relay_hosts": ["h"]}
    out = sources.with_v2_blocks(raw["config"], raw)
    assert out["schema"] == {"sources": 2} and out["sources_v2"] == [{"id": "x"}] and out["match"] == "word"
    assert out["category_names"] == {"a": "A"} and "relay_hosts" not in out and raw["config"] == {"sources": [1]}
    inside = sources.with_v2_blocks({"sources": [1], "match": "legacy"}, raw)
    assert inside["match"] == "legacy"  # config wins


def test_keyword_check_respects_the_switch_and_never_uses_exclude():
    base = {"keywords": ["seed"], "categories": {"wildlife": ["tiger"]},
            "sources": [{"id": "a", "name": "A", "kind": "rss", "type": "news", "url": "https://a.example/feed",
                         "filter": False, "keywords": ["own"]}]}
    lists = as_v2(base)
    lists["sources_v2"][0]["check"] = {"roles": ["include"], "categories": ["wildlife"]}
    rel = {"strong": ["animal welfare"], "exclude": ["bull market"]}
    assert sources.load_sources_data(lists, relevance=rel)[0][0].keywords == ["own"]  # switch off: no check
    lists["sources_v2"][0]["filter"] = True
    lists["sources_v2"][0]["check"] = {"roles": ["include", "exclude"], "categories": []}
    assert sources.load_sources_data(lists, relevance=rel)[0][0].keywords == ["animal welfare"]  # exclude ignored
    # no relevance lists (sources.yaml fallback): Include reads the lists' keywords, which the site fills with it
    assert sources.load_sources_data(lists)[0][0].keywords == ["seed"]


def test_gate_prefers_the_lists_category_names(monkeypatch):
    class Client:
        def relevance_config(self):
            raise OSError("down")

    gate = relevance.Gate.from_site(None, Client(), fallback={"wildlife": "Wildlife"}, cache_file=Path("/nonexistent/x.json"))
    assert gate.categories == {"wildlife": "Wildlife"}
