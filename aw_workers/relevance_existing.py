"""Relevance check of the items already on the site (owner, 10 Oct 2026).

Reads News, Library, Resources (posts, Research included), Opportunities and Press — drafts and published — from the
authenticated endpoint aw/v1/relevance-items, runs the same gate as the pipelines (relevance.py), and stores each
verdict on the item (meta aw_relevance) through aw/v1/relevance-verdicts. Nothing is trashed, published or changed
otherwise. An item no provider could check gets no verdict (run again with only_missing to finish it).

Run: python -m aw_workers.relevance_existing   (needs WP_* secrets and provider keys)
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import ingest, relevance, summarise
from .run import truthy

log = logging.getLogger("aw_workers.relevance_existing")

# USD per million tokens (input, output) for the usual cheap models; an estimate from text length (4 chars ≈ 1 token).
PRICES = {"openai/gpt-4o-mini": (0.15, 0.60), "gpt-4o-mini": (0.15, 0.60), "deepseek-chat": (0.27, 1.10),
          "claude-haiku-4-5": (1.00, 5.00)}
DOC_CATEGORIES = {"guide", "faq", "how-to"}  # our own guides: no outside issuer


def kind_of(row: dict) -> str:
    t = row.get("type")
    if t == "post":
        cats = set(row.get("categories") or [])
        if "research" in cats:
            return "research"
        return "guide" if cats & DOC_CATEGORIES else "resource"
    return {"aw_news": "news", "aw_legal": "legal", "aw_opportunity": "opportunity", "aw_press": "press"}.get(t, "news")


def candidate(row: dict) -> relevance.Candidate:
    return relevance.Candidate(kind=kind_of(row), title=row.get("title", ""), url=row.get("source_url", ""),
                               text=row.get("summary", ""), source=row.get("source_name", ""), issuer=row.get("issuer", ""))


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    only_missing = truthy(os.environ.get("AW_ONLY_MISSING"), default=False)
    threads = int(os.environ.get("AW_THREADS", "6"))
    client = ingest.Client(os.environ["WP_BASE_URL"], os.environ["WP_INGEST_USER"], os.environ["WP_INGEST_APP_PASSWORD"])
    backends = summarise.backends_from_env(os.environ, ledger={})
    if not backends:
        log.error("no provider key set")
        return 2
    for b in backends:  # one job, many threads: the providers' own rate limits apply (429s back off)
        if getattr(b, "min_interval", 0) and b.name != "gemini":
            b.min_interval = 0.0
    gate = relevance.Gate.from_site(summarise.Summariser(backends), client)
    gate.cache = {}  # fresh verdicts for existing items
    gate.client = None  # existing items are not added to the Rejected log; their verdict is stored on the item

    rows, page = [], 1
    while True:
        data = client.relevance_items(page, 200, missing_only=only_missing)
        rows += data.get("items") or []
        if page >= int(data.get("pages") or 1):
            break
        page += 1
    log.info("%d item(s) to check", len(rows))
    started = time.time()
    chars_in = Counter()
    chars_out = Counter()

    def check(row):
        c = candidate(row)
        v = gate.classify(c)
        if v is None:
            return row, None
        ok, why = gate.decide(c, v)
        prompt_len = 2200 + len(c.title) + min(len(c.text), 1500)
        chars_in[v.model] += prompt_len
        chars_out[v.model] += 60 + len(v.reason)
        return row, {"id": row["id"], "relevant": ok, "category": gate.category_slug(v.category), "india": v.india,
                     "issuer": v.issuer, "reason": why, "model": v.model, "at": v.at, "model_relevant": v.relevant}

    counts: Counter = Counter()
    pending: list[dict] = []
    out_rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=threads) as pool:
        for row, verdict in pool.map(check, rows):
            if verdict is None:
                counts["unavailable"] += 1
                continue
            counts["pass" if verdict["relevant"] else "fail"] += 1
            pending.append(verdict)
            out_rows.append({**{k: row.get(k) for k in ("id", "type", "status", "title", "source_url")}, **verdict})
            if len(pending) >= 50:
                client.relevance_verdicts(pending)
                pending = []
    if pending:
        client.relevance_verdicts(pending)

    minutes = (time.time() - started) / 60
    cost = 0.0
    for model, n in chars_in.items():
        pin, pout = PRICES.get(model.split(":", 1)[-1], (0.15, 0.60))
        cost += n / 4 / 1e6 * pin + chars_out[model] / 4 / 1e6 * pout
    used = Counter(r["model"] for r in out_rows)
    summary = {"items": len(rows), **counts, "minutes": round(minutes, 1), "estimated_usd": round(cost, 4), "models": dict(used)}
    Path("relevance-existing-report.json").write_text(json.dumps({"summary": summary, "items": out_rows}, ensure_ascii=False, indent=1))
    print(json.dumps(summary))
    step = os.environ.get("GITHUB_STEP_SUMMARY")
    if step:
        with open(step, "a") as fh:
            fh.write(f"### Relevance check of existing items\n\n```\n{json.dumps(summary, indent=1)}\n```\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
