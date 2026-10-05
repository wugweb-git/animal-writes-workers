"""Test many AI keys at once, keep the working ones, optionally load them into GitHub secrets.

1. Put your keys in ~/.ai-keys.txt, one per line, as   provider=key
   (several keys for the same provider are fine). Example lines:
       anthropic=sk-ant-api03-...
       gemini=AIza...
       groq=gsk_...
   Providers: anthropic, gemini, groq, openrouter, openai, deepseek, xai, moonshot
   Blank lines and lines starting with # are ignored.
2. Run:  python3 scripts/sort_keys.py            (test only)
         python3 scripts/sort_keys.py --github   (also set ANTHROPIC/GEMINI/GROQ secrets
                                                   from the first working key of each)
Results show only the last 4 characters of each key. Working keys are written to
~/.ai-keys.working (permissions 600); ~/.ai-keys.txt is left unchanged.
Checks call free "list models" endpoints; no tokens are spent.
"""

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from check_keys import PROVIDERS, check  # noqa: E402

SRC = Path.home() / ".ai-keys.txt"
OUT = Path.home() / ".ai-keys.working"
REPO = "wugweb-git/animal-writes-workers"
SECRETS = {"anthropic": "ANTHROPIC_API_KEY", "gemini": "GEMINI_API_KEY", "groq": "GROQ_API_KEY"}
ENDPOINTS = {name.lower(): (url, style) for name, url, style in PROVIDERS}


def main() -> int:
    if not SRC.exists():
        print(f"Create {SRC} first (see the top of this script).")
        return 1
    working, seen = [], set()
    for n, raw in enumerate(SRC.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        provider, _, key = line.partition("=")
        provider, key = provider.strip().lower(), key.strip().strip('"')
        if provider not in ENDPOINTS or not key:
            print(f"line {n}: skipped (unknown provider '{provider}')")
            continue
        if (provider, key) in seen:
            continue
        seen.add((provider, key))
        result = check(*ENDPOINTS[provider], key)
        print(f"{provider:<10} …{key[-4:]}  {result}")
        if result.startswith("✅") or "429" in result:
            working.append((provider, key))

    OUT.write_text("".join(f"{p}={k}\n" for p, k in working))
    os.chmod(OUT, 0o600)
    print(f"\n{len(working)} working key(s) saved to {OUT}")

    if "--github" in sys.argv:
        for provider, secret in SECRETS.items():
            key = next((k for p, k in working if p == provider), None)
            if key:
                subprocess.run(["gh", "secret", "set", secret, "-R", REPO], input=key.encode(), check=True)
            else:
                print(f"{secret}: no working {provider} key, left unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
