"""Research stops each paper and the whole run in time (2 Nov 2026: run 41438340089 spent 15-20 minutes on each of four
papers whose providers all timed out, and GitHub cancelled the job at 120 minutes before the report was written)."""

import json
from datetime import date

import pytest
import requests

from aw_workers import ingest, research, research_titles, summarise
from aw_workers.sources import Item, Source


class Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr("time.monotonic", c)
    return c


class SlowBackend:
    """Never answers: each call runs into its 60 s read timeout (the clock moves on by that much)."""

    model = "m"
    min_interval = 0.0
    api_key = "k"

    def __init__(self, clock, n=0):
        self.clock, self.name, self.calls = clock, f"slow{n}", 0

    def ask(self, prompt: str) -> str:
        self.calls += 1
        self.clock.t += 60
        raise requests.Timeout("Read timed out. (read timeout=60)")


def item() -> Item:
    src = Source(id="research", name="Research", kind="api", type="research", url="")
    return Item(source=src, title="Rabies in dogs in Kerala, India", url="https://doi.org/10.1/x",
                date=date(2026, 10, 1), text="A study of rabies in dogs in Kerala. " * 20, publisher="Journal")


def test_a_paper_whose_providers_keep_timing_out_stops_at_its_time_budget(clock):
    backends = [SlowBackend(clock, n) for n in range(150)]
    s = summarise.Summariser(backends, fetch=lambda url: "", coverage=lambda *a: [])
    start = clock.t
    s.deadline = start + 6 * 60
    with pytest.raises(summarise.OutOfTime):
        s.summarise(item())
    calls = sum(b.calls for b in backends)
    assert calls == 6  # six 60 s timeouts fill the six minutes, long before the 30-try cap
    assert clock.t - start == 6 * 60


def test_resting_keys_that_wake_after_the_budget_are_not_waited_for(clock, monkeypatch):
    class Limited(SlowBackend):
        def ask(self, prompt):
            self.calls += 1
            raise requests.HTTPError("429 Client Error: Too Many Requests")

    slept = []
    monkeypatch.setattr(summarise.time, "sleep", lambda s: slept.append(s))
    s = summarise.Summariser([Limited(clock, 0), Limited(clock, 1)], fetch=lambda url: "", coverage=lambda *a: [])
    s.deadline = clock.t + 30  # the keys rest 60 s: past the budget
    with pytest.raises(summarise.OutOfTime):
        s.summarise(item())
    assert slept == []


def test_without_a_deadline_other_workers_keep_the_30_try_cap(clock):
    """Run, backfill, press, summary rewrite: no deadline is set, so nothing changes for them."""
    backends = [SlowBackend(clock, n) for n in range(150)]
    s = summarise.Summariser(backends, fetch=lambda url: "", coverage=lambda *a: [])
    assert s.deadline is None
    with pytest.raises(summarise.SummaryError) as exc:
        s._summarise(item())
    assert not isinstance(exc.value, summarise.OutOfTime)
    assert "stopped after 30 tries" in str(exc.value)
    assert sum(b.calls for b in backends) == 30


def test_categories_and_titles_stop_when_the_time_is_spent(clock):
    backends = [SlowBackend(clock, n) for n in range(5)]
    s = summarise.Summariser(backends)
    s.deadline = clock.t - 1
    assert summarise.pick_categories(s, item(), "summary", ["rabies"]) == []
    assert research_titles.draft(s, [{"id": 1, "study_title": "t", "abstract": "a", "summary": "s"}]) == {}
    assert sum(b.calls for b in backends) == 0
    s.deadline = None
    assert not summarise.out_of_time(s)


# ---- the run ----

def papers(n: int) -> list[dict]:
    return [{"title": f"Rabies in dogs in Kerala, India, part {i}", "abstract": "Rabies in dogs in Kerala, India.",
             "affiliation": "", "date": "2026-10-01", "url": f"https://doi.org/10.1/p{i}", "journal": "J",
             "authors": ["A B"]} for i in range(n)]


class FakeClient:
    def __init__(self, *a, **k):
        self.posted = []

    def policy(self):
        return {}

    def exists(self, dois, urls, index=False):
        return {"dois": [], "urls": [], "studies": []}

    def post(self, payload):
        self.posted.append(payload)
        return "created"


class FakeGate:
    def admit(self, c):
        return "pass", None

    def save(self):
        pass


@pytest.fixture
def run(monkeypatch, tmp_path, clock):
    """main() with the network replaced: each summary takes `minutes[url]` minutes (default 3); a paper listed in
    `hang` keeps failing until its time is spent."""
    monkeypatch.chdir(tmp_path)
    for k, v in {"DRY_RUN": "false", "WP_BASE_URL": "https://x.test", "WP_INGEST_USER": "u", "WP_INGEST_APP_PASSWORD": "p",
                 "AW_RESEARCH_DAYS": "30", "AW_RESEARCH_PAPER_MIN": "6", "AW_RESEARCH_RUN_MIN": "20", "AW_RESEARCH_HEADLINE_MIN": "1"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.delenv("RUNNER_TEMP", raising=False)
    client = FakeClient()
    monkeypatch.setattr(ingest, "Client", lambda *a, **k: client)
    monkeypatch.setattr(research.relevance.Gate, "from_site", classmethod(lambda cls, *a, **k: FakeGate()))
    monkeypatch.setattr(research.summary_rules, "use", lambda c: None)
    monkeypatch.setattr(summarise, "backends_from_env", lambda env, ledger=None: ["b"])
    monkeypatch.setattr(summarise, "pick_categories", lambda *a, **k: [])
    monkeypatch.setattr(research, "headline_fields",
                        lambda s, p, summary: {"title": "Rabies in Kerala dogs", "seo_title": "Rabies", "focus_keyword": "rabies", "slug": "rabies"})
    state = {"hang": set(), "papers": papers(10), "summary_min": 3}

    class FakeSummariser:
        def __init__(self, backends):
            self.backends, self.deadline, self.dead, self.cool = backends, None, set(), {}

        def summarise(self, it):
            if it.url in state["hang"]:
                while not summarise.out_of_time(self):
                    clock.t += 60  # every provider times out
                raise summarise.OutOfTime("slow0: Read timed out | time spent after 6 tries")
            clock.t += state["summary_min"] * 60
            return "A summary."

    monkeypatch.setattr(summarise, "Summariser", FakeSummariser)
    monkeypatch.setattr(research, "collect", lambda floor, today: state["papers"])
    monkeypatch.setattr(research, "_STARTED", clock.t)
    state["client"] = client
    return state


def test_the_run_deadline_stops_new_papers_writes_the_report_and_exits_0(run, tmp_path):
    assert research.main() == 0
    report = json.loads((tmp_path / "research-report.json").read_text())
    # papers start at 0, 3, 6, 9 and 12 minutes (12 + 6 + 1 <= 20); at 15, 15 + 7 > 20: not started
    assert report["counts"]["sent"] == 5
    assert report["counts"]["not reached"] == 5
    assert [r["result"] for r in report["deferred"]] == ["not reached"] * 5
    known = json.loads((tmp_path / research.KNOWN_FILE).read_text())
    assert known == sorted(f"10.1/p{i}" for i in range(5))  # the papers not reached come back next run


def test_a_skipped_paper_is_not_marked_known_and_the_run_moves_on(run, tmp_path):
    run["papers"] = papers(3)
    run["hang"] = {"https://doi.org/10.1/p0"}
    assert research.main() == 0
    report = json.loads((tmp_path / "research-report.json").read_text())
    assert report["counts"]["skipped"] == 1
    assert report["counts"]["sent"] == 2
    assert report["counts"]["failed"] == 0
    assert report["deferred"][0]["url"] == "https://doi.org/10.1/p0"
    known = json.loads((tmp_path / research.KNOWN_FILE).read_text())
    assert "10.1/p0" not in known and "10.1/p1" in known
    assert [p["source_url"] for p in run["client"].posted] == ["https://doi.org/10.1/p1", "https://doi.org/10.1/p2"]


def test_the_job_summary_shows_the_deferred_counts(run, tmp_path, monkeypatch):
    out = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(out))
    research.main()
    text = out.read_text()
    assert "not reached (run time limit): 5" in text
    assert "Skipped (over 6 min each): 0" in text


def test_a_summary_that_uses_the_whole_paper_budget_still_gets_its_headline_and_is_sent(run, monkeypatch, clock):
    # run 41505586844 (2 Nov 2026): 5 summaries written, then lost to "time spent before the headline"
    run["papers"] = papers(1)
    run["summary_min"] = 6  # the summary takes all of the paper's 6 minutes
    seen = []

    def headline(s, p, summary):
        seen.append(summarise.out_of_time(s))
        return {"title": "Rabies in Kerala dogs", "seo_title": "Rabies", "focus_keyword": "rabies", "slug": "rabies"}

    monkeypatch.setattr(research, "headline_fields", headline)
    assert research.main() == 0
    assert seen == [False]  # the headline had time of its own
    assert len(run["client"].posted) == 1 and not run["client"].posted[0].get("hold")


def test_a_headline_that_does_not_come_in_its_time_is_held_for_a_person_not_lost(run, monkeypatch, clock):
    run["papers"] = papers(1)

    def no_headline(s, p, summary):
        clock.t += 2 * 60  # longer than its 1 minute
        return {}

    monkeypatch.setattr(research, "headline_fields", no_headline)
    assert research.main() == 0
    posted = run["client"].posted
    assert len(posted) == 1 and research.NO_HEADLINE in posted[0]["hold"]
