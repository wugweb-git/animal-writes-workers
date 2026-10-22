"""One short request to each GitHub Models model (owner, 22 Oct 2026): which ones the workflow's token can use today.
Run by .github/workflows/github-models-check.yml (by hand). Prints one line per model; exits 1 when none answers."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from aw_workers import summarise  # noqa: E402


def main() -> int:
    backends = summarise.github_models(os.environ)
    if not backends:
        print("GITHUB_MODELS_TOKEN is not set")
        return 1
    ok = 0
    for b in backends:
        try:
            reply = b.ask("Summarise in one sentence: The Animal Welfare Board of India issued new guidelines for stray dog sterilisation in Delhi.")
            ok += 1
            print(f"OK   {b.model}: {reply[:100]}")
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL {b.model}: {str(exc)[:160]}")
    print(f"{ok} of {len(backends)} models answer")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
