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
    from aw_workers import relay, sources

    sources.SECTIONS.clear()
    relay.configure(None, ())
    yield
    sources.SECTIONS.clear()
    relay.configure(None, ())
