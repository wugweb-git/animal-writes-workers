# Animal Writes – sourcing workers

Finds Indian animal-welfare news and legal notices, summarises each with an AI model,
and sends them to the Animal Writes site, where they land as **Pending Review**. Nothing is published automatically.

## How it runs
GitHub Actions (`.github/workflows/sourcing.yml`): every 6 hours, plus **Run workflow** by hand.
Tests run separately (`tests.yml`, on every push and after Sources sync) and do not block sourcing: a failing test
opens an issue assigned to the owner (since 22 Oct 2026).

Every workflow reports its start and end to the site (`.github/actions/report`, `POST aw/v1/worker-run`): result,
failed or skipped step, counts, commit. The site shows them in Settings → Sourcing → Workers and emails the owner
when a run fails or a scheduled one has not reported for twice its interval. The list is `docs/workers.json`.

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
Each of these secrets may hold **several keys, one per line**. A key that is rejected, out of credit
or rate-limited is skipped for the rest of the run and the next key is used.
`python3 scripts/sort_keys.py --github` tests a list of keys and loads every working one.

## Summary providers
Tried in the order of the repository **variable** `LLM_PROVIDERS` (default `haiku,gemini,groq`; this repo sets `gemini,groq,haiku` so the free tiers are used up first).
Haiku comes first for quality, capped at an estimated **$4/month** (variable `HAIKU_MONTHLY_BUDGET_USD`);
once the cap is reached it is skipped until the next month. Gemini, then Groq, are used only if the one before it errors, is over budget,
or its summary still fails the checks after one retry. Every provider must pass the same checks.
Optional variables: `GEMINI_MODEL` (default `gemini-flash-latest`), `GROQ_MODEL` (default `openai/gpt-oss-120b`).
Each run logs how many summaries each provider wrote. Free tiers have rate limits, and on
Gemini's free tier Google may use prompts to improve its models (the input here is public news).

## Sources
Edit `sources.yaml`. Its `categories:` section maps each site category (by slug) to keywords; items are filed
under every category whose keywords appear in the title or feed text. AWBI and PIB items also send a `source_type`. Kinds: `rss` (any feed) and `awbi` (AWBI table pages). `type` maps to the site:
`news` → News, `legal` → Legal. Legal summaries end with `[Needs legal review]`.
Court news (Supreme Court, High Courts, NGT) comes from LiveLaw, Bar & Bench and SC Observer, tagged by the court named in the headline.

Notes:
- PIB's own RSS refuses automated clients (403), so PIB comes via a Google News query limited to `pib.gov.in`.
- Google News links are resolved to the publisher's own URL, and each article's preview image (og:image) is linked, never downloaded.
- AWBI items are summarised from their titles only (the PDFs are not read).

## Summary rules (checked in code, one retry, otherwise the item fails)
2–3 sentences, at most 80 words, no quote over 15 words, no run of more than 15 words copied from the source.

## Search Insights: rising terms
`search-insights.yml` runs `aw_workers/search_insights.py` (Search Insights plan §5.3, phase 3). Free sources only:
- daily 08:10 IST: Google Trends "Trending now" RSS (India; kept only when the term or its news names one of the site's keywords, topics or species), Wikipedia pageviews for a watchlist (2× the 30-day average; at most one request a second, user-agent with a contact), GDELT timelines of Indian news (3× the 14-day average; 5 s between requests);
- weekly, Monday 08:30 IST: Google autocomplete in English, gl=in (2–4 s between requests; snapshots in `.state/search-insights/`, a new suggestion is rising), Trends rising related queries;
- monthly, the 1st, 09:00 IST: Trends interest over 12 months, "animal welfare" = 100.

Trends and autocomplete get one paced attempt a run; a refusal is reported to the site and the owner pastes the figures on the Rising tab. The lists come from `GET aw/v1/search-insights`; each source's results go to `POST aw/v1/search-insights`. Scheduled runs stay dry until the repository variable `SI_LIVE` is `true` (set it once the site route is live).
`DRY_RUN=true python -m aw_workers.search_insights all --days 7 --top 10` prints the top rising terms.

## Local
```
pip install -r requirements-dev.txt
pytest -q
DRY_RUN=true python -m aw_workers.run
```
