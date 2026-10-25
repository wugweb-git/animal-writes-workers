from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixture_text():
    return lambda name: (FIXTURES / name).read_text(encoding="utf-8-sig")


@pytest.fixture(autouse=True)
def _no_coverage_search(monkeypatch):
    """Tests never search Google News: the other-coverage fallback finds nothing unless a test gives it results."""
    from aw_workers import coverage

    def offline(url):
        raise OSError("offline in tests")

    monkeypatch.setattr(coverage, "_get_feed", offline)


@pytest.fixture(autouse=True)
def _fresh_section_lists():
    """Each test starts without the site's per-section lists (a test loading sources.yaml, which has them since the
    lists are copied from the site, must not change what later tests classify)."""
    from aw_workers import matcher, relay, sources

    def reset():  # also the matching mode and the keyword-table switch a loaded sources.yaml (or policy) may have set
        sources.SECTIONS.clear()
        relay.configure(None, ())
        matcher.set_mode(None)
        sources.KEYWORD_TABLE["on"] = False

    reset()
    yield
    reset()


@pytest.fixture(autouse=True)
def _no_run_counts(monkeypatch):
    """Tests never write this run's counts (aw_workers/counts.py) into the real Actions run's report."""
    monkeypatch.delenv("RUNNER_TEMP", raising=False)


@pytest.fixture(autouse=True)
def _frozen_summary_rules(monkeypatch):
    """Tests read the summary rules of 15 Oct 2026 (tests/fixtures/summary_rules_defaults.json), not
    aw_workers/summary_rules.json, which Sources sync copies from the site from phase 3 (owner, 25 Oct 2026): the
    owner's edits on the site never break the tests that run after the sync. Each test also starts with the fallback
    section map (no sections_v2 read by an earlier test)."""
    import json

    from aw_workers import sections, summary_rules

    monkeypatch.setattr(summary_rules, "DEFAULTS", json.loads((FIXTURES / "summary_rules_defaults.json").read_text(encoding="utf-8")))
    summary_rules.reset()
    sections.use(None)
    yield
    summary_rules.reset()
    sections.use(None)
