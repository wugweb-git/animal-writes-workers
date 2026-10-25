"""Sourcing restructure phase 3, Rules (owner, 25 Oct 2026): sections from the site's post types (`sections_v2` with
`worker_type`), summary rules by post type (`by_type`), and the Directory enrichment field rules and trust order from
GET enrich/config, with the old code maps and merge.py's constants as the fallback."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import yaml

from aw_workers import sections, sources, summary_rules
from aw_workers.directory import merge
from aw_workers.directory.model import CHECK, Context, Fact

ROOT = Path(__file__).resolve().parent.parent
SEED = json.loads((ROOT / "tests" / "fixtures" / "enrich_field_rules_seed.json").read_text(encoding="utf-8"))
YAML = yaml.safe_load((ROOT / "sources.yaml").read_text(encoding="utf-8"))
WORKER_TYPES = {"aw_news": "news", "aw_legal": "legal", "post": "research", "aw_opportunity": "opportunity",
                "aw_press": "press"}


@pytest.fixture(autouse=True)
def clean():
    """Each test starts with merge.py's constants (conftest resets the summary rules and the section map)."""
    merge.reset()
    yield
    merge.reset()


def as_v2_sections(v1: dict, extra: dict | None = None) -> list[dict]:
    """What the site's policy sends from phase 3: one entry per section, built from the same option as the v1 dict."""
    out = []
    for pt, sec in {**v1, **(extra or {})}.items():
        out.append({"post_type": pt, "label": pt, "worker_type": WORKER_TYPES.get(pt, ""),
                    "categories": list(sec.get("categories") or []), "tags": list(sec.get("tags") or []),
                    "tag_rules": [], "summary": pt})
    return out


# ---------- field rules ----------

def strip_apply(rule: dict) -> dict:
    return {k: v for k, v in rule.items() if k != "apply"}


def test_seed_fixture_equals_the_constants():
    """The site seeded aw_enrich_field_rules from these constants; the shared fixture must equal them exactly."""
    assert SEED["priority"] == merge.PRIORITY
    for field, rule in SEED["field_rules"].items():
        assert strip_apply(rule) == merge.rule_from_constants(field), field
    named = set(merge.MULTI) | merge.AI_FIELDS | set(merge.INVITE_FIELDS) | merge.SOCIAL_FIELDS | merge.OFFICIAL_AWBI
    assert named <= set(SEED["field_rules"]), "every field a constant names has a seeded rule"
    assert len(SEED["field_rules"]) == 42


def test_rules_from_the_seed_equal_the_built_in_rules():
    site = merge.rules_from_config({"field_rules": SEED["field_rules"], "priority": SEED["priority"]})
    built_in = merge.rules_from_config({})
    assert site.fields is not None and built_in.fields is None
    for field in list(SEED["field_rules"]) + ["something_new"]:
        assert site.rule(field) == built_in.rule(field), field
    assert site.priority == built_in.priority == merge.PRIORITY


def ctx_with(*facts, now=None):
    c = Context({"id": 1, "name": "Blue Cross of India", "district": "Chennai", "state": "Tamil Nadu", "now": now or {},
                 "protected": False})
    c.add(*facts)
    return c


CASES = [
    (Fact("email", "a@x.org", "website", "W"), Fact("email", "a@x.org", "osm", "O"), Fact("founded", "1999", "website", "W")),
    (Fact("email", "a@x.org", "website", "W"), Fact("email", "b@x.org", "osm", "O")),
    (Fact("phone", "98765 43210", "website", "W"), Fact("phone", "91234 56789", "darpan", "D"),
     Fact("phone", "99887 76655", "osm", "O"), Fact("hours", [{"day": "Monday"}], "google_places", "G", role=CHECK)),
    (Fact("services", ["rescue"], "website", "W"), Fact("services", ["rescue"], "social", "S"),
     Fact("animals", "dogs", "website", "W"), Fact("animals", "dogs", "claim", "C")),
    (Fact("address", "1 Road, Chennai", "awbi", "A"), Fact("pincode", "600001", "awbi", "A"),
     Fact("registration", "R-1", "awbi", "A")),
    (Fact("instagram", "https://instagram.com/bc", "website", "W"), Fact("instagram", "https://instagram.com/bc", "social", "S"),
     Fact("facebook", "https://facebook.com/bc", "website", "W"), Fact("facebook", "https://facebook.com/bc", "osm", "O")),
]


@pytest.mark.parametrize("facts", CASES)
def test_proposals_with_the_seeded_site_rules_equal_the_constants(facts):
    """Seeding changes nothing: the same facts give the same proposals and the same invitations."""
    want = merge.proposals(ctx_with(*copy.deepcopy(facts)))
    merge.use_config({"field_rules": SEED["field_rules"], "priority": SEED["priority"], "schema": {}})
    got = merge.proposals(ctx_with(*copy.deepcopy(facts)))
    for side in (want, got):
        for p in side[0]:
            p["sources"] = [{k: v for k, v in s.items() if k != "fetched_at"} for s in p["sources"]]
    assert got == want


def test_a_field_added_in_wp_admin_is_off_until_its_rule_is_set():
    """A new ACF field shows in Field rules with its rule off: nothing is proposed for it until the owner turns it on."""
    rules = dict(SEED["field_rules"], aw_tiktok={"on": False, "sources": [], "multi": 0, "official": "", "ai": False,
                                                 "invite": False, "social": True})
    merge.use_config({"field_rules": rules, "priority": SEED["priority"]})
    c = ctx_with(Fact("aw_tiktok", "https://tiktok.com/@bc", "website", "W"), Fact("email", "a@x.org", "website", "W"))
    assert [p["field"] for p in merge.proposals(c)[0]] == ["email"]
    rules["aw_tiktok"]["on"] = True
    merge.use_config({"field_rules": rules, "priority": SEED["priority"]})
    c = ctx_with(Fact("aw_tiktok", "https://tiktok.com/@bc", "website", "W"))
    assert [(p["field"], p["confidence"]) for p in merge.proposals(c)[0]] == [("aw_tiktok", "medium")]


def test_edited_rules_take_effect():
    rules = copy.deepcopy(SEED["field_rules"])
    rules["email"]["multi"] = 2  # two different emails may be proposed, and disagreeing is no longer low
    rules["registration"]["official"] = "darpan"  # NGO Darpan alone is the official record of the registration
    rules["founded"]["sources"] = ["wikidata"]  # only Wikidata may fill the year
    rules["head"]["on"] = False
    merge.use_config({"field_rules": rules, "priority": ["claim", "osm", "website"]})
    c = ctx_with(Fact("email", "a@x.org", "website", "W"), Fact("email", "b@x.org", "osm", "O"),
                 Fact("registration", "R-1", "darpan", "D"), Fact("founded", "1999", "website", "W"),
                 Fact("head", "A Person", "website", "W"))
    props = merge.proposals(c)[0]
    emails = [p for p in props if p["field"] == "email"]
    assert [p["value"] for p in emails] == ["b@x.org", "a@x.org"]  # the edited trust order puts OSM first
    assert all(p["confidence"] == "medium" for p in emails)
    assert {p["field"]: p["confidence"] for p in props}["registration"] == "high"
    assert "founded" not in {p["field"] for p in props} and "head" not in {p["field"] for p in props}


def test_unusable_site_rules_fall_back_value_by_value():
    r = merge.rules_from_config({"field_rules": {"phone": {"multi": "two", "ai": "yes", "on": True}, "email": "x"},
                                 "priority": "not a list"})
    assert r.rule("phone")["multi"] == 0 and r.rule("phone")["ai"] is False  # bad values: the default rule's
    assert r.rule("email") == merge.DEFAULT_RULE
    assert r.priority == merge.PRIORITY


# ---------- sections ----------

def test_sections_v2_give_the_same_allowed_lists_as_v1():
    v1 = YAML["sections"]
    sources.load_sources_data(copy.deepcopy(YAML))
    before = {t: (sources.allowed_categories(t), sources.allowed_tags(t)) for t in ("news", "legal", "opportunity", "research")}
    lists = copy.deepcopy(YAML)
    lists["sections_v2"] = as_v2_sections(v1, {"aw_press": {"categories": [], "tags": []}})
    sources.load_sources_data(lists)
    after = {t: (sources.allowed_categories(t), sources.allowed_tags(t)) for t in ("news", "legal", "opportunity", "research")}
    assert after == before
    assert sources.allowed_categories("press") == list(sources.CATEGORIES)  # aw_press: empty = every category
    assert sections.post_type_for("press") == "aw_press" and sections.post_type_for("research") == "post"


def test_worker_types_come_from_the_site():
    v2 = [{"post_type": "aw_reports", "worker_type": "research", "categories": ["wildlife"], "tags": []}]
    assert sections.use(v2)
    assert sections.post_type_for("research") == "aw_reports"
    assert sections.post_type_for("news") == "aw_news"  # the fallback map for what the site does not name
    assert sections.post_type_for("aw_campaign") == "aw_campaign"
    assert not sections.use([{"label": "no post type"}])  # malformed: back to the code map
    assert sections.post_type_for("research") == "post"


def test_sources_yaml_without_sections_v2_resets_the_map():
    sections.use([{"post_type": "aw_reports", "worker_type": "research"}])
    sources.load_sources_data(copy.deepcopy({k: v for k, v in YAML.items() if k != "sections_v2"}))
    assert sections.post_type_for("research") == "post"


def test_sections_v2_is_kept_in_the_sources_yaml_copy():
    raw = {"config": {"sources": []}, "sections_v2": [{"post_type": "aw_news"}]}
    assert sources.with_v2_blocks(raw["config"], raw)["sections_v2"] == [{"post_type": "aw_news"}]


# ---------- summary rules by post type ----------

LIVE = {  # the site's v1 sections on 25 Oct 2026 (word counts), as served beside by_type after the phase 3 migration
    "news": {"label": "News", "min_words": 100, "max_words": 200, "min_sentences": 4},
    "library": {"label": "Library", "min_words": 160, "max_words": 300},
    "research": {"label": "Research", "min_words": 160, "max_words": 300},
}


def site_rules(**extra_types):
    by_type = {
        "aw_news": {**LIVE["news"], "alias": "news"},
        "aw_legal": {**LIVE["library"], "alias": "library"},
        "post": {**LIVE["research"], "alias": "research"},
        "aw_opportunity": {**LIVE["news"], "label": "Opportunities", "inherit": "aw_news"},
        "aw_press": {**LIVE["news"], "label": "Media & PR", "inherit": "aw_news"},
    }
    by_type.update(extra_types)
    return {"sections": copy.deepcopy(LIVE), "by_type": by_type}


def test_rule_by_post_type():
    r = summary_rules.merged(site_rules())
    assert [summary_rules.section_for(k, r) for k in ("news", "aw_news", "legal", "aw_legal", "research", "post")] == \
        ["news", "news", "library", "library", "research", "research"]
    assert summary_rules.section_for("opportunity", r) == "news"  # inherits News, as before phase 3
    assert summary_rules.section_for("press", r) == "news"
    assert r["sections"]["news"]["min_words"] == 100 and "min_sentences" not in r["by_type"]["aw_news"]


def test_without_by_type_the_v1_map_applies():
    r = summary_rules.merged({"sections": copy.deepcopy(LIVE)})
    assert "by_type" not in r
    for k in ("news", "aw_news", "legal", "aw_legal", "research", "post", "opportunity", ""):
        assert summary_rules.section_for(k, r) == summary_rules.SECTION_OF_TYPE.get(k, "news")


def test_a_section_with_its_own_rule():
    """A new content type that stops inheriting has its own rule, read for its items."""
    r = summary_rules.merged(site_rules(aw_campaign={"label": "Campaign Hub", "min_words": 30, "max_words": 60,
                                                     "structure": "Lead.", "attribution": "Say who."}))
    sections.use([{"post_type": "aw_campaign", "worker_type": "campaign"}])
    assert summary_rules.section_for("campaign", r) == "aw_campaign"
    assert r["sections"]["aw_campaign"]["max_words"] == 60
    found = summary_rules.problems(" ".join(["word"] * 70) + ".", "aw_campaign", rules=r)
    assert any("over 60 words" in p for p in found)
    assert "Lead." in summary_rules.rules_block("aw_campaign", r)


def test_by_type_wins_over_the_v1_section():
    site = site_rules()
    site["by_type"]["aw_news"]["max_words"] = 180
    assert summary_rules.merged(site)["sections"]["news"]["max_words"] == 180


def test_for_file_reads_like_the_defaults():
    out = summary_rules.for_file({**site_rules(), "version": "2026-10-26"})
    assert out["version"] == "2026-10-26"
    assert all("min_sentences" not in s for s in out["sections"].values())
    assert all("min_sentences" not in s for s in out["by_type"].values())
    assert summary_rules.merged(out) == out  # the file read back gives the same rules


# ---------- Sources sync writes the two new files ----------

class FakeClient:
    def __init__(self, rules, cfg):
        self.rules, self.cfg = rules, cfg

    def summary_rules(self):
        return self.rules

    def _call(self, method, path, **kw):
        assert (method, path) == ("GET", "/enrich/config")
        return self.cfg


def test_sync_writes_summary_rules_and_directory_schema(tmp_path, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("sync_sources", ROOT / "scripts" / "sync_sources.py")
    sync = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sync)
    monkeypatch.setenv("AW_SUMMARY_RULES", str(tmp_path / "summary_rules.json"))
    monkeypatch.setenv("AW_DIRECTORY_SCHEMA", str(tmp_path / "directory_schema.json"))
    cfg = {"sources": {}, "schema": {"group": "group_aw_listing", "fields": []}, "priority": SEED["priority"],
           "field_rules": SEED["field_rules"], "counts": {"total": 1}}
    client = FakeClient(site_rules(), cfg)
    assert sync.sync_summary_rules(client) is True and sync.sync_summary_rules(client) is False
    assert sync.sync_directory_schema(client) is True and sync.sync_directory_schema(client) is False
    saved = json.loads((tmp_path / "directory_schema.json").read_text(encoding="utf-8"))
    assert set(saved) == {"schema", "priority", "field_rules"}  # no counts or spend in the copy
    assert merge.rules_from_config(saved).fields == merge.rules_from_config(cfg).fields
    rules = json.loads((tmp_path / "summary_rules.json").read_text(encoding="utf-8"))
    assert rules["sections"]["library"]["min_words"] == 160 and rules["by_type"]["post"]["alias"] == "research"
    assert sync.sync_directory_schema(FakeClient({}, {"sources": {}})) is None  # a site before phase 3
    assert sync.sync_summary_rules(FakeClient({}, {})) is None
