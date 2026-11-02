"""Every workflow that hands an AI key to a job also hands it the provider order and the free providers' keys.

31 Oct 2026: press-gnews.yml passed GEMINI_API_KEY and OPENROUTER_API_KEY but no LLM_PROVIDERS and no Groq key, so
backends_from_env built Gemini only; its 429s left 18 of 30 items "classifier unavailable"."""
from pathlib import Path

import pytest
import yaml

from aw_workers import summarise

WORKFLOWS = sorted((Path(__file__).resolve().parents[1] / ".github" / "workflows").glob("*.yml"))
NEEDED = ("LLM_PROVIDERS", "GROQ_API_KEY", "GROQ_MODEL", "GEMINI_MODEL", "OPENROUTER_API_KEY")


def _ai_job_envs():
    for wf in WORKFLOWS:
        data = yaml.safe_load(wf.read_text(encoding="utf-8")) or {}
        for name, job in (data.get("jobs") or {}).items():
            env = dict(data.get("env") or {}) | dict(job.get("env") or {})
            if "GEMINI_API_KEY" in env:
                yield f"{wf.name}:{name}", env


@pytest.mark.parametrize("label,env", list(_ai_job_envs()), ids=lambda v: v if isinstance(v, str) else "")
def test_ai_jobs_get_the_provider_order_and_free_keys(label, env):
    missing = [k for k in NEEDED if k not in env]
    assert not missing, f"{label} lacks {', '.join(missing)}"


def test_press_gnews_builds_more_than_gemini(monkeypatch):
    monkeypatch.setattr(summarise, "site_rows", lambda env, session=None: [])
    env = {"GEMINI_API_KEY": "g", "GROQ_API_KEY": "q", "OPENROUTER_API_KEY": "o",
           "LLM_PROVIDERS": "openrouter,gemini,groq"}
    names = [b.name for b in summarise.backends_from_env(env, ledger={})]
    assert names[0] == "openrouter" and "groq" in names and "gemini" in names
