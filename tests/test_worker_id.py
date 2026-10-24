from aw_workers import firecrawl, summarise
from aw_workers.worker_id import ai_keys_params, worker_id

REF = "wugweb-git/animal-writes-workers/.github/workflows/summary-rewrite.yml@refs/heads/main"
SITE = {"WP_BASE_URL": "https://example.test", "WP_INGEST_USER": "u", "WP_INGEST_APP_PASSWORD": "p"}


def test_explicit_id_wins():
    assert worker_id({"AW_WORKER_ID": " research ", "GITHUB_WORKFLOW_REF": REF}) == "research"


def test_id_from_workflow_ref():
    assert worker_id({"GITHUB_WORKFLOW_REF": REF}) == "summary-rewrite"
    assert worker_id({"GITHUB_WORKFLOW_REF": "o/r/.github/workflows/sourcing.yml@refs/pull/7/merge"}) == "sourcing"
    assert worker_id({"GITHUB_WORKFLOW_REF": "o/r/.github/workflows/news-title-case.yaml"}) == "news-title-case"


def test_no_id_when_unknown():
    assert worker_id({}) == ""
    assert worker_id({"AW_WORKER_ID": " ", "GITHUB_WORKFLOW_REF": "not a workflow path"}) == ""
    assert ai_keys_params({}) == {}
    assert ai_keys_params({"GITHUB_WORKFLOW_REF": REF}) == {"worker": "summary-rewrite"}


class _Resp:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


class _Session:
    def __init__(self, data):
        self.data, self.calls = data, []

    def get(self, url, **kw):
        self.calls.append((url, kw))
        return _Resp(self.data)


def test_summarise_sends_worker_param():
    s = _Session({"rows": [{"provider": "gemini", "key": "a"}]})
    summarise.site_rows({**SITE, "AW_WORKER_ID": "sourcing"}, session=s)
    url, kw = s.calls[0]
    assert url == "https://example.test/wp-json/aw/v1/ai-keys" and kw["params"] == {"worker": "sourcing"}


def test_summarise_omits_worker_param_without_id():
    s = _Session({"rows": []})
    summarise.site_rows(SITE, session=s)
    url, kw = s.calls[0]
    assert url.endswith("/wp-json/aw/v1/ai-keys") and not kw.get("params")


def test_firecrawl_sends_worker_param_from_ref():
    s = _Session({"tools": [{"provider": "firecrawl", "key": "fc-x"}]})
    assert firecrawl.site_key({**SITE, "GITHUB_WORKFLOW_REF": REF}, session=s) == "fc-x"
    assert s.calls[0][1]["params"] == {"worker": "summary-rewrite"}


def test_firecrawl_omits_worker_param_without_id():
    s = _Session({"tools": []})
    firecrawl.site_key(SITE, session=s)
    assert "?" not in s.calls[0][0] and not s.calls[0][1].get("params")
