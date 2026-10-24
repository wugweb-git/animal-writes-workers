"""docs/workers.json (the site's Workers tab) must match the workflows: name, schedule, timeout, longest gap."""
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
REG = json.loads((ROOT / "docs/workers.json").read_text())["workers"]
FILES = sorted(p.name for p in (ROOT / ".github/workflows").glob("*.yml"))


def _wf(name):
    d = yaml.safe_load((ROOT / ".github/workflows" / name).read_text())
    on = d.get("on", d.get(True)) or {}  # PyYAML reads the key on as True
    job = next(iter(d["jobs"].values()))
    return d, on, job


def _field(spec, lo, hi):
    out = set()
    for part in spec.split(","):
        rng, _, step = part.partition("/")
        a, b = (lo, hi) if rng == "*" else (tuple(int(x) for x in rng.split("-")) if "-" in rng else (int(rng), int(rng) if not step else hi))
        out |= set(range(a, b + 1, int(step or 1)))
    return out


def _minutes_in_week(cron):
    """Minutes since Monday 00:00 UTC when a cron fires (minute, hour, day of week; the 1st-of-month job is skipped)."""
    m, h, dom, mon, dow = cron.split()
    days = range(7) if dow == "*" else [(int(d) - 1) % 7 for d in _field(dow, 0, 6)]
    return sorted(d * 1440 + hh * 60 + mm for d in days for hh in _field(h, 0, 23) for mm in _field(m, 0, 59))


def test_every_workflow_is_listed():
    assert sorted(REG) == FILES


def test_names_schedules_timeouts_match():
    for f in FILES:
        d, on, job = _wf(f)
        r = REG[f]
        assert r["name"] == d["name"], f
        assert sorted(c["cron"] for c in on.get("schedule") or []) == sorted(r["crons"]), f
        assert int(job.get("timeout-minutes", 360)) == r["timeout_min"], f
        after = (on.get("workflow_run") or {}).get("workflows") or []
        assert after == ([r["after"]] if r.get("after") else []), f


def test_interval_is_the_longest_gap():
    for f, r in REG.items():
        if not r["crons"] or any(c.split()[2] != "*" for c in r["crons"]):  # monthly: given by hand
            continue
        t = sorted(set(x for c in r["crons"] for x in _minutes_in_week(c)))
        gaps = [b - a for a, b in zip(t, t[1:])] + [t[0] + 7 * 1440 - t[-1]]
        assert max(gaps) == r["interval_min"], f


def test_every_workflow_reports_start_and_end():
    for f in FILES:
        _, _, job = _wf(f)
        steps = [s for s in job["steps"] if s.get("uses") == "./.github/actions/report"]
        phases = [s["with"]["phase"] for s in steps]
        assert phases[-1] == "end" and steps[-1].get("if") == "always()", f
        assert f == "wp-cron.yml" or phases[0] == "start", f
        main = steps[-1]["with"]["main"]
        assert any(s.get("id") == main for s in job["steps"]), f


def test_late_limits_cover_the_schedule():
    """Every workflow states its late limit; a scheduled one's limit is longer than its longest gap."""
    for f, r in REG.items():
        assert "late_after_h" in r, f
        if r["late_after_h"] is not None:
            assert r["late_after_h"] * 60 > r.get("interval_min", 0), f
