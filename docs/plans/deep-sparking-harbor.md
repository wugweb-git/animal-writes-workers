> Status as of 24 Nov 2026 (checked against git): Mostly open. Workers repo still has AW_WINDOW default 2d; no tools.yml; darpan-import, backfill etc. still present. Workers-repo changes need the owner's go-ahead.

# Workers plan: status 17 Nov 2026

Checked 17 Nov on git (`origin/wordpress-dev`, workers `origin/main`), GitHub runs and the live site (read-only).
Rules: no PRs unless you ask; the workers repo only with your go-ahead (given 5 Nov); side sessions don't push or ship; AI keys are yours; merges only on "merge workers #N".

## Where each part stands
| Part | What | Status 17 Nov |
|---|---|---|
| A | Audit (read-only) | **Done 3 Nov.** `Claude outputs/workers-audit-2026-11-02.md`; Notion "Workers" (now under SourceWeb, updated 17 Nov). |
| B | Summariser re-queue (site) | **Built, rebased 17 Nov, handed to the shipping session** ("1 · Design, UX, UI"): branch `workers-site-1117`, `44eaa902`. Not live yet. |
| C | Workers repo changes | **Built 5 Nov**, branch `workers-cleanup-1105` (`fad2120`, 6 commits, 576 tests passed). Not pushed: needs a PR and your "merge workers #N". Workers `main` has moved only by Sources sync copies since, so no rebase expected. |
| D | Site alerts + registry | Backup alert: in `workers-site-1117` (with B). Cron alert: already live (no change needed). Registry at 12: branch `workers-registry-1105` (`7c991a52`), **ships only after C is merged**; needs a rebase first (955 commits behind). |

## Live facts (17 Nov)
- Every worker's last 6 runs succeeded (10–16 Nov). Research ran fine on 9 and 16 Nov.
- Summariser: **1,205** published summaries below their section minimum, none queued (News 982/1,757 below 100 words, Library 102/157 below 160, Research 121/153 below 160).
- UpdraftPlus to Google Drive: daily, last 16 Nov 04:25 UTC. No alert live yet.
- Cron in 48 h: Hostinger 568 runs, GitHub "WordPress cron" 8.
- `AW_SCHEDULER` is not set, so the site scheduler is off; GitHub's schedule runs everything, and Sourcing is also started from Hostinger every 4 h.
- Registry: 19 workflows on the site and in the workers repo.

## Part B: Summariser re-queue (branch `workers-site-1117`)
- Cause: `aw_summary_rw_settled()` never compared a summary with the current minimum, so ok/done items settled under the old minimums never came back.
- Fix: every state keeps `min_words`; an ok/done item below the current minimum that wasn't judged at it is open again. History kept, nothing deleted, never padded; a thin source ends as short and is listed. `?progress=1` counts `below_min` per section.
- Check after ship: `?progress=1` shows about 1,205 `below_min`, then falls run by run; spot-check 5 rewrites (history kept, date unchanged, not padded).

## Part C: workers repo (branch `workers-cleanup-1105`)
1. Sourcing window 3 days (`run.py`, `sourcing.yml`).
2. Research subjects from the site's `research_subjects` (beside or inside `config`), code list as fallback; the log says which. `concurrency: research`.
3. Focus keywords: "English only, no Hindi" note (behaviour unchanged).
4. Paid AI key lines and the Haiku / Google Places budget variables no longer passed to any job. Secrets stay; deleting them is yours.
5. Crawler check: `scheduled_cron` input + `AW_SCHEDULER` guard.
6. Directory enrichment: not started by a skipped SEO scores run (no double run under the site scheduler).
7. Tools (by hand): `tools.yml` with task `submit` or `relevance-check`; each report names its task; failures alert and email like every workflow.
8. Removed: submit, relevance-existing (now Tools tasks), backfill, news-title-case, same-story-fold, press-gnews, darpan-import, wp-cron. `docs/workers.json` at 12. Python modules kept.
- Trust rules: no code. Disagreement → low → review is already the code; set Darpan ID, registration and NGO type to `apply: review` in SourceWeb → Rules (settings save).
- Check after merge: next Sourcing log shows `when:3d`; next Research run logs where its subjects came from; no paid key names in the workflows.

## Part D: registry (branch `workers-registry-1105`)
- Registry at 12 workflows; `AW_TICK_SKIP` without wp-cron; cron box and cron alert text drop the GitHub trigger.
- Order: merge C → rebase D on wordpress-dev → checks → hand to the shipping session. Shipping it before C fails the Deploy check (the registry comparison).

## Order from here
1. Shipping session ships `workers-site-1117` (B + backup alert). Then I check `?progress=1` and the Workers problems on live.
2. Push `workers-cleanup-1105`, open the workers PR (on your word), then your "merge workers #N".
3. Rebase `workers-registry-1105`, checks, hand over, ship.
4. Yours: delete the unused paid AI secrets (Anthropic, OpenAI, xAI, DeepSeek, Moonshot, MiniMax) if you want; Darpan identity fields → `apply: review`; after step 3, `AW_SCHEDULER=site` and the hPanel tick.
5. 25 Nov: revisit the Search Insights thresholds (Wikipedia 2×, GDELT 3×).

## Rules
Read-only on live (SSH read-only); secrets by name only; free only; AI keys report only; don't touch Lab, HC source, Opportunities, Community dogs; no copy written; tell you before handing work to agents or sessions; `docs/pending.md` after each done item.
