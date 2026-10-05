"""Paste any AI keys, find out which provider each belongs to, whether it works and its models.

1. Put keys in ~/.ai-keys.txt, ONE KEY PER LINE. Labels are optional:
       AIza...                 (provider detected automatically)
       gemini=AIza...          (or say it explicitly)
   Anything else on a line (names, notes, "export X=") is ignored if it doesn't look like a key.
2. Run:  python3 scripts/sort_keys.py            (test only)
         python3 scripts/sort_keys.py --github   (also load ALL working Anthropic/Gemini/Groq keys
                                                   into the workers' GitHub secrets)
Only the last 4 characters of each key are ever printed. Working keys are saved to
~/.ai-keys.working (permissions 600). Checks use free "list models" calls; no tokens are spent.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from check_keys import PROVIDERS, fetch_models  # noqa: E402

SRC = Path.home() / ".ai-keys.txt"
OUT = Path.home() / ".ai-keys.working"
REPO = "wugweb-git/animal-writes-workers"
SECRETS = {"anthropic": "ANTHROPIC_API_KEY", "gemini": "GEMINI_API_KEY", "groq": "GROQ_API_KEY"}
ENDPOINTS = {name.lower(): (url, style) for name, url, style in PROVIDERS}

# Key shape -> likely providers, most specific first.
PATTERNS = [
    (r"sk-ant-api\d\d-[\w-]{20,}", ["anthropic"]),
    (r"sk-ant-[\w-]{20,}", ["anthropic"]),
    (r"sk-or-v1-[0-9a-f]{20,}", ["openrouter"]),
    (r"gsk_[A-Za-z0-9]{20,}", ["groq"]),
    (r"AIza[\w-]{30,}", ["gemini"]),
    (r"xai-[A-Za-z0-9]{20,}", ["xai"]),
    (r"sk-proj-[\w-]{20,}", ["openai"]),
    (r"sk-[0-9a-f]{32}", ["deepseek", "openai"]),
    (r"sk-[A-Za-z0-9_-]{20,}", ["moonshot", "openai", "deepseek"]),
]


def find_keys(text: str):
    """Yield (explicit_provider_or_None, key) for every key-looking token in the text."""
    seen = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        label = None
        m = re.match(r"^([A-Za-z]+)\s*[=:]\s*", line)
        if m and m.group(1).lower() in ENDPOINTS:
            label = m.group(1).lower()
        for pattern, _ in PATTERNS:
            for key in re.findall(pattern, line):
                if key not in seen and not any(key in k for k in seen):
                    seen.add(key)
                    yield label, key


def candidates(label, key):
    if label:
        return [label]
    for pattern, providers in PATTERNS:
        if re.fullmatch(pattern, key):
            return providers
    return []


def main() -> int:
    if not SRC.exists():
        print(f"Create {SRC} first (see the top of this script).")
        return 1
    working = []
    for label, key in find_keys(SRC.read_text()):
        tail = f"…{key[-4:]}"
        provs = candidates(label, key)
        if key.startswith("sk-ant-") and not key.startswith("sk-ant-api"):
            print(f"{'anthropic':<10} {tail}  ❌ not an API key (Claude account token); use one from console.anthropic.com")
            continue
        result, models = "⚠️ unknown key type", None
        for prov in provs:
            result, models = fetch_models(*ENDPOINTS[prov], key)
            if not result.startswith("❌"):
                break
        prov = prov if provs else "?"
        line = f"{prov:<10} {tail}  {result}"
        if models:
            shown = ", ".join(sorted(models)[:8])
            line += f"\n{'':<16}{len(models)} models: {shown}{' …' if len(models) > 8 else ''}"
        print(line)
        if result.startswith("✅") or "429" in result:
            working.append((prov, key))

    OUT.write_text("".join(f"{p}={k}\n" for p, k in working))
    os.chmod(OUT, 0o600)
    print(f"\n{len(working)} working key(s) saved to {OUT}")

    if "--github" in sys.argv:
        for provider, secret in SECRETS.items():
            keys = [k for p, k in working if p == provider]
            if keys:
                subprocess.run(["gh", "secret", "set", secret, "-R", REPO], input="\n".join(keys).encode(), check=True)
                print(f"{secret}: {len(keys)} working key(s) set")
            else:
                print(f"{secret}: no working {provider} key, left unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
