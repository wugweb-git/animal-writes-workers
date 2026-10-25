# Animal Writes sourcing: what the workers read and how to control it

This file documents exactly what the sourcing workers fetch. The configuration lives in `sources.yaml`; publisher and keyword controls live in WP Admin (see below).

## Site lists, v1 and v2 (sourcing restructure, phase 1, 25 Oct 2026)

The site's sourcing policy (`GET aw/v1/sourcing-policy`) keeps every v1 key (`publishers`, `relevance`, `config`,
`relay_hosts`) and adds new blocks beside them, inside `config` or at the top level: `schema` (`{"sources": 2}`),
`sources_v2`, `category_names` and, from phase 2, `match`. Sources sync copies them into `sources.yaml`.

- `sources_v2`: each source as in v1 plus `host` (its publisher), `relay` (fetched through the site), `read_by`
  (`code` or `feed`) and `check` (`{roles: [...], categories: [...]}`). The workers read it when it is there and every
  entry is complete, and the v1 `sources` list otherwise; both build the same sources and searches (tests/test_sources_v2.py).
- Relay hosts: the policy's `relay_hosts` plus the host of every `sources_v2` source whose relay switch is on.
- `category_names`: slug => name; the relevance check's categories when the site cannot be reached.
- `match: "word"`: per-source keyword checks, category words and topic tags match whole words (plural allowed, a
  trailing `*` matches the start of a word, Hindi as substrings; aw_workers/matcher.py, tests/fixtures/match_cases.json,
  the same file the site tests). Until the site sends it, the old matching stays.

## Limits

- Items older than **14 days** are ignored.
- At most **15 items per source** per run.
- Runs every 6 hours (`17 */6`) plus a backup run (`47 2-23/6`). Every item lands in WordPress as **Pending Review**; nothing is published without a person.

## Sources

| id | Name | Type | Kind | URL | Keyword filter |
|---|---|---|---|---|---|
| `gnews-animal-welfare` | Google News – animal welfare India | news | rss | `https://news.google.com/rss/search?q=%22animal+welfare%22+India&hl=en-IN&gl=IN&ceid=IN:en` | none |
| `gnews-animal-cruelty` | Google News – animal cruelty India | news | rss | `https://news.google.com/rss/search?q=%22animal+cruelty%22+India&hl=en-IN&gl=IN&ceid=IN:en` | none |
| `the-hindu-environment` | The Hindu – Energy & Environment | news | rss | `https://www.thehindu.com/sci-tech/energy-and-environment/feeder/default.rss` | animal keywords |
| `awbi-circulars` | Animal Welfare Board of India – Circulars | legal (awbi) | awbi | `https://awbi.gov.in/circulars` | none |
| `awbi-notifications` | Animal Welfare Board of India – Notifications | legal (awbi) | awbi | `https://awbi.gov.in/notifications` | none |
| `livelaw-top` | LiveLaw | legal (auto-court) | rss | `https://www.livelaw.in/category/top-stories/google_feeds.xml` | animal keywords |
| `barandbench` | Bar & Bench | legal (auto-court) | rss | `https://www.barandbench.com/feed` | animal keywords |
| `court-news-animals` | Court news (LiveLaw, Bar & Bench, SC Observer) | legal (auto-court) | rss | `https://news.google.com/rss/search?q=(site:livelaw.in+OR+site:barandbench.com+OR+site:scobserver.in)+(animal+OR+animals+OR+dogs+OR+cattle+OR+cow+OR+wildlife+OR+cruelty+OR+stray)&hl=en-IN&gl=IN&ceid=IN:en` | animal keywords |
| `pib-releases` | Press Information Bureau | legal (pib) | rss | `https://news.google.com/rss/search?q=site:pib.gov.in+(animal+OR+rabies+OR+veterinary+OR+livestock+OR+wildlife)&hl=en-IN&gl=IN&ceid=IN:en` | animal keywords |

Google News queries, decoded:

- `gnews-animal-welfare`: `"animal welfare" India`
- `gnews-animal-cruelty`: `"animal cruelty" India`
- `court-news-animals`: `(site:livelaw.in OR site:barandbench.com OR site:scobserver.in) (animal OR animals OR dogs OR cattle OR cow OR wildlife OR cruelty OR stray)`
- `pib-releases`: `site:pib.gov.in (animal OR rabies OR veterinary OR livestock OR wildlife)`

## Animal keyword filter (sources marked "animal keywords")

An item is kept if its title or summary contains any of: `animal welfare`, `animal cruelty`, `cruelty to animals`, `stray`, `street dog`, `community dog`, `cattle`, `cow shelter`, `gaushala`, `wildlife`, `veterinary`, `rabies`, `animal birth control`, `ABC Rules`, `PCA Act`, `Prevention of Cruelty`, `AWBI`, `Animal Welfare Board`, `पशु`, `जानवर`, `पशुपालन`, `रेबीज`.

## Categories

Categories label items; they do not filter anything.

| Category | Keywords |
|---|---|
| `community-dogs` | stray dog, street dog, community dog, stray dogs, street dogs, community animals, कुत्त |
| `animal-birth-control` | animal birth control, abc rules, abc programme, abc program, sterilis, steriliz, neuter |
| `rabies-public-health` | rabies, रेबीज, dog bite, anti-rabies, vaccination drive |
| `cruelty-prosecution` | cruelty, fir, arrested, prosecution, torture, abuse, poison |
| `feeders-caregivers` | feeder, feeding, caregiver |
| `cattle-gaushalas` | cattle, cow, gaushala, gaushalas, bovine, बैल, गाय |
| `transport-slaughter` | slaughter, transportation of animals, illegal transport, smuggl |
| `wildlife` | wildlife, tiger, leopard, elephant, pangolin, forest department, poach, sanctuary, national park |
| `birds` | bird, birds, avian, pigeon, kite, peacock |
| `pets-breeding` | pet shop, breeder, breeding, dog breed, pet |
| `adoption` | adopt, adoption |
| `performing-captive-animals` | circus, performing animals, captive, zoo, elephant ride |
| `laws-rules` | act, rules, amendment, notification, guidelines, circular, gazette |
| `courts-judgments` | supreme court, high court, court, judgment, bench, petition, tribunal |
| `policy-government` | ministry, government, minister, municipal, corporation, policy, scheme, cabinet |
| `disaster-rescue` | flood, cyclone, earthquake, disaster, rescue operation |
| `veterinary-care` | veterinary, vet, hospital, clinic |
| `campaigns-civic-action` | campaign, petition, protest, letter, signature, awareness |

## Controls in WP Admin: News → Sourcing

WordPress is the source of truth for publisher and keyword policy. The workers read it before every run (`GET /wp-json/aw/v1/sourcing-policy`, authenticated) and skip refused items before summarising them; the ingest endpoint checks again on arrival, so a block takes effect even if a worker is out of date.

**Publishers tab.** Every host the workers send is recorded automatically with its name, item count and first/last seen dates. Each host is:

- **Neutral**: the default for new finds. Items are accepted (subject to the keyword filter when it is on).
- **Allowed**: trusted. Items skip the keyword filter.
- **Blocked**: items are refused (`status: blocked`).

Change one or many at once, add a host by hand, or use **Block {host}** on any row in the News and Legal Watch lists.

**Keywords tab.** Four editable lists, one term per line (Hindi works):

- **Strong**: one is enough to keep an item.
- **Animal** + **Context**: an item with one of each is kept.
- **Exclude**: always dropped (figurative or unrelated uses).

Switches (both off by default):

- **Relevance filter on**: apply the keyword rules to items from Neutral publishers.
- **Only allowed publishers from aggregator feeds**: Google News, Apple News and similar feeds (feed ids starting `gnews-`, `applenews-`, `agg-`) accept only Allowed publishers.

A **Test a headline** box shows whether a headline would be kept and why.

Run summaries report `fetched / new / duplicate / skipped / failed`; `skipped` counts items refused by this policy.

## Directory (one-off imports from public lists)

Imported with `aw-wordpress/tools/directory/` (idempotent; unverified listings with the source and list date shown).

| Source | Date of list | What came in | Imported |
|---|---|---|---|
| AWBI list of recognised animal welfare organisations | 31 May 2026 | name, AWBI code, district, state | 3,630 listings |
| AWBI ABC project recognition list (`awbi.gov.in/view/index/Project-Recognition-for-ABC`) | 29 Jun 2026 | organisation, ABC centre, district, state, validity | 29 new listings; 35 existing organisations marked as ABC centres |
| DAHD reply to Lok Sabha unstarred question 2854 (1962 Mobile Veterinary Units, Annexure I) | 18 Mar 2025 | units per state | 28 state listings, phone 1962 |
| Tamil Nadu vet hospitals/dispensaries (data.gov.in, GODL-India) | 2018 | district, place | not imported: downloads need a registered data.gov.in API key |

## Opportunities (approved 7 Oct 2026)

Polled with the other sources every 6 hours. Items are **published directly** as factual listings (title, organisation, place, deadline when given, link to the organisation's page); the summary line is built from those fields, not written by AI. Closed calls (deadline passed) are skipped.

| Source | Reader | What comes in | Filter |
|---|---|---|---|
| WWF-India careers (`wwfindia.zohorecruit.com/jobs/careers`) | `zoho` (jobs JSON embedded in the page) | title, city, state, date opened | job title must name conservation work (fundraising, design, e-commerce roles are left out) |
| AWBI Notifications (`awbi.gov.in/Notifications`) | `awbi` | award invitations, vacancies, internships (title from the PDF name) | invitation / award / vacancy / recruitment / internship wording; deadline read from the title |
| Wildlife SOS careers (`wildlifesos.org/about-us/careers/`) | fetched **by the site** once a day (`opportunities-site.php`), because Cloudflare refuses GitHub's servers | role, location | all roles (animal organisation) |

Later, after their terms are read: NGOBox (grants, CSR, RFPs) and DevNetJobsIndia, behind an animal keyword filter. Not used: LinkedIn, Instagram (terms forbid), PETA India jobs and Animal Rahat (bot-blocked), FIAPO (blocks AI crawlers).
