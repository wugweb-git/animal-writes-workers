# Animal Writes – sourcing workers

Finds Indian animal-welfare news and legal notices, summarises each with an AI model,
and sends them to the Animal Writes site, where they land as **Pending Review**. Nothing is published automatically.

## How it runs
GitHub Actions (`.github/workflows/sourcing.yml`): every 6 hours, plus **Run workflow** by hand.
Tests run first; the worker only runs if they pass.

**DRY_RUN is ON by default.** It fetches and summarises but sends nothing.
- Scheduled runs: go live by setting the repository **variable** `DRY_RUN` = `false`.
- Manual runs: untick "Dry run".

Each run reports `fetched / new / duplicate / failed` in the job summary. A rejected login (401/403) stops the run and fails it.

## Secrets (Settings → Secrets and variables → Actions)
| Secret | Value |
|---|---|
| `WP_BASE_URL` | Site address, e.g. `https://slategray-donkey-829925.hostingersite.com` |
| `WP_INGEST_USER` | The `ingest` WordPress user (Editor role) |
| `WP_INGEST_APP_PASSWORD` | That user's Application Password |
| `ANTHROPIC_API_KEY` | Anthropic API key (Claude Haiku 4.5, paid, about $1–2/month) |
| `GEMINI_API_KEY` | Google AI Studio key (Gemini, free tier) |
| `GROQ_API_KEY` | Groq key (free tier) |

At least one of the three summary keys is needed; providers without a key are skipped.

## Summary providers
Tried in the order of the repository **variable** `LLM_PROVIDERS` (default `haiku,gemini,groq`).
Haiku comes first for quality; Gemini, then Groq, are used only if the one before it errors
or its summary still fails the checks after one retry. Every provider must pass the same checks.
Optional variables: `GEMINI_MODEL` (default `gemini-flash-latest`), `GROQ_MODEL` (default `llama-3.3-70b-versatile`).
Each run logs how many summaries each provider wrote. Free tiers have rate limits, and on
Gemini's free tier Google may use prompts to improve its models (the input here is public news).

## Sources
Edit `sources.yaml`. Kinds: `rss` (any feed) and `awbi` (AWBI table pages). `type` maps to the site:
`news` → News, `legal` → Legal. Legal summaries end with `[Needs legal review]`.
Supreme Court / High Court sources: v2.

Notes:
- PIB's own RSS refuses automated clients (403), so PIB comes via a Google News query limited to `pib.gov.in`.
- Google News items link to `news.google.com` article URLs, not the publisher's.
- AWBI items are summarised from their titles only (the PDFs are not read).

## Summary rules (checked in code, one retry, otherwise the item fails)
2–3 sentences, at most 80 words, no quote over 15 words, no run of more than 15 words copied from the source.

## Local
```
pip install -r requirements-dev.txt
pytest -q
DRY_RUN=true python -m aw_workers.run
```
