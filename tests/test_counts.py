import json

from aw_workers import counts


def test_write_merges_and_skips_outside_actions(tmp_path, monkeypatch):
    monkeypatch.delenv("RUNNER_TEMP", raising=False)
    counts.write(new=1)  # nothing to write to: no error
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    counts.write(fetched=10, new=2)
    counts.write(new=3, bad=[1], lists="site")
    assert json.loads((tmp_path / "aw-counts.json").read_text()) == {"fetched": 10, "new": 3, "lists": "site"}
