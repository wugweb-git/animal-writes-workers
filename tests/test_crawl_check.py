"""Crawler-access check (aw_workers/crawl_check.py): verdicts, sitemap fallback, the run and its payload."""
import requests

from aw_workers import crawl_check as cc

PAGE = '<html><head><link rel="canonical" href="https://animalwrites.in/x/"></head><body>animalwrites.in</body></html>'
CHALLENGE = "<html><head><title>Just a moment...</title></head><body>Verify you are human</body></html>"


class Resp:
    def __init__(self, status=200, text=PAGE):
        self.status_code = status
        self.text = text
        self.content = text.encode()


def test_verdicts():
    assert cc.verdict(200, PAGE) == "ok"
    assert cc.verdict(403, PAGE) == "refused"
    assert cc.verdict(429, "") == "refused"
    assert cc.verdict(200, CHALLENGE) == "challenge"
    assert cc.verdict(200, "<html>a blank page</html>") == "challenge"  # not one of our pages
    assert cc.verdict(200, '<html><link href="/wp-content/themes/x.css"></html>') == "ok"  # a noindex page of ours


def test_fetch_records_network_errors_as_refused():
    def get(url, **kw):
        raise requests.ConnectionError("reset")

    got = cc.fetch("https://animalwrites.in/", "UA", get)
    assert got["result"] == "refused" and got["code"] == 0 and got["error"] == "ConnectionError"


def test_sitemap_pages_pick_one_of_each_kind_and_a_burst():
    def get(url, **kw):
        if "post-sitemap" in url:
            locs = ["https://animalwrites.in/about/", "https://animalwrites.in/resources/", "https://animalwrites.in/resources/rabies/", "https://animalwrites.in/resources/feeding/"]
        elif "aw_news" in url:
            raise requests.Timeout("slow")
        else:
            kind = url.rsplit("/", 1)[1].split("-")[0]
            locs = [f"https://animalwrites.in/{kind}/{i}/" for i in range(6)]
        img = "<image:image><image:loc>https://animalwrites.in/wp-content/uploads/x.avif</image:loc></image:image>"
        body = ("<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9' xmlns:image='http://www.google.com/schemas/sitemap-image/1.1'>"
                + "".join(f"<url><loc>{u}</loc>{img}</url>" for u in locs) + "</urlset>")
        return Resp(text=body)

    plan = cc.sitemap_pages("https://animalwrites.in", get)
    kinds = {p["kind"]: p["url"] for p in plan["pages"]}
    assert set(kinds) == {"home", "listing", "library", "resource", "opportunity"}  # news dropped: its sitemap failed
    assert kinds["resource"] == "https://animalwrites.in/resources/rabies/"  # the /resources/ index is not a guide
    assert not any("uploads" in u for u in plan["burst"])  # image:loc entries are not pages
    assert len(plan["burst"]) <= cc.BURST_SIZE and plan["burst"][0] == "https://animalwrites.in/"


def test_run_checks_every_agent_and_bursts_two():
    seen = []

    def get(url, headers=None, **kw):
        seen.append((headers["User-Agent"], url))
        if "GPT" in headers["User-Agent"] and url.endswith("/b/"):
            return Resp(403, "Forbidden")
        return Resp()

    plan = {"pages": [{"kind": "home", "url": "https://animalwrites.in/"}, {"kind": "news", "url": "https://animalwrites.in/b/"}], "burst": ["https://animalwrites.in/", "https://animalwrites.in/b/"]}
    agents = {"Browser": "Mozilla Chrome", "GPTBot": "GPTBot/1.2", "ClaudeBot": "ClaudeBot/1.0"}
    sleeps = []
    res = cc.run(plan, agents, get, sleeps.append)
    assert res["runner"] == "github"
    assert len(res["checks"]) == 6
    bad = [c for c in res["checks"] if c["result"] != "ok"]
    assert [(c["agent"], c["kind"], c["code"]) for c in bad] == [("GPTBot", "news", 403)]
    assert [b["agent"] for b in res["burst"]] == ["Browser", "GPTBot"]
    assert res["burst"][1] == {"agent": "GPTBot", "n": 2, "ok": 1, "codes": {"200": 1, "403": 1}, "max_ms": res["burst"][1]["max_ms"]}
    assert sleeps == [cc.GAP] * 3  # one pause between the two pages of each agent
    table = cc.table(res)
    assert "| GPTBot | 1/2 | news 403 refused |" in table


def test_main_dry_run_sends_nothing(monkeypatch, capsys):
    for k in ("WP_BASE_URL", "WP_INGEST_USER", "WP_INGEST_APP_PASSWORD", "GITHUB_STEP_SUMMARY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setattr(cc, "sitemap_pages", lambda base: {"pages": [{"kind": "home", "url": base + "/"}], "burst": []})
    monkeypatch.setattr(cc, "fetch", lambda url, ua, get=None: {"code": 200, "ms": 5, "bytes": 10, "result": "ok"})
    assert cc.main([]) == 0
    assert "| Googlebot | 1/1 | — |" in capsys.readouterr().out


def test_main_live_needs_secrets(monkeypatch):
    for k in ("WP_BASE_URL", "WP_INGEST_USER", "WP_INGEST_APP_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DRY_RUN", "false")
    assert cc.main([]) == 1


def test_run_stops_at_the_deadline_and_says_so():
    t = iter(range(0, 10000, 100))  # every clock reading is 100 s later
    plan = {"pages": [{"kind": "home", "url": "https://animalwrites.in/"}] * 3, "burst": ["https://animalwrites.in/"]}
    res = cc.run(plan, {"Browser": "UA", "GPTBot": "GPTBot"}, lambda u, **kw: Resp(), lambda s: None, lambda: next(t), deadline=250)
    assert res["cut"] is True and len(res["checks"]) < 6 and res["burst"] == []
    assert "stopped after" in cc.table(res)
