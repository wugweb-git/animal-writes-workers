"""Check which AI provider keys still work, without printing them.

Run:  python3 scripts/check_keys.py
Paste each key when asked (input is hidden); press Enter to skip a provider.
Each check calls the provider's free "list models" endpoint; no tokens are spent.
"""

import getpass
import json
import urllib.error
import urllib.request

PROVIDERS = [
    # name, URL, how the key is sent
    ("Anthropic", "https://api.anthropic.com/v1/models", "anthropic"),
    ("Gemini", "https://generativelanguage.googleapis.com/v1beta/models", "google"),
    ("Groq", "https://api.groq.com/openai/v1/models", "bearer"),
    ("OpenRouter", "https://openrouter.ai/api/v1/key", "bearer"),
    ("OpenAI", "https://api.openai.com/v1/models", "bearer"),
    ("DeepSeek", "https://api.deepseek.com/models", "bearer"),
    ("xAI", "https://api.x.ai/v1/models", "bearer"),
    ("Moonshot", "https://api.moonshot.ai/v1/models", "bearer"),
]


def fetch_models(url: str, style: str, key: str):
    """Returns (status_text, model_ids or None)."""
    headers = {"User-Agent": "key-check"}
    if style == "bearer":
        headers["Authorization"] = f"Bearer {key}"
    elif style == "anthropic":
        headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
    elif style == "google":
        headers["x-goog-api-key"] = key
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403) or (style == "google" and e.code == 400):
            return f"❌ rejected (HTTP {e.code})", None
        if e.code == 429:
            return "⚠️ valid but rate-limited / out of quota (HTTP 429)", None
        return f"⚠️ HTTP {e.code}", None
    except Exception as e:
        return f"⚠️ could not check: {type(e).__name__}", None
    items = data.get("data") or data.get("models") or []
    ids = []
    if isinstance(items, list):
        for m in items:
            if isinstance(m, dict):
                ids.append(str(m.get("id") or m.get("name") or "").replace("models/", ""))
    return "✅ works", [i for i in ids if i]


def check(url: str, style: str, key: str) -> str:
    headers = {"User-Agent": "key-check"}
    if style == "bearer":
        headers["Authorization"] = f"Bearer {key}"
    elif style == "anthropic":
        headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
    elif style == "google":
        headers["x-goog-api-key"] = key
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20) as r:
            json.load(r)
            return "✅ works"
    except urllib.error.HTTPError as e:
        if e.code in (401, 403) or (style == "google" and e.code == 400):  # Gemini: 400 = key not valid
            return f"❌ rejected (HTTP {e.code})"
        if e.code == 429:
            return "⚠️ valid but rate-limited / out of quota (HTTP 429)"
        return f"⚠️ HTTP {e.code}"
    except Exception as e:  # network etc.
        return f"⚠️ could not check: {type(e).__name__}"


def main() -> None:
    for name, url, style in PROVIDERS:
        key = getpass.getpass(f"{name} key (Enter to skip): ").strip()
        if key:
            print(f"  {name}: {check(url, style, key)}")
        del key


if __name__ == "__main__":
    main()
