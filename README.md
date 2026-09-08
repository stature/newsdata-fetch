# newsdata-fetch — Stage 1 Forestry news collector

A small scheduled batch job (no MCP, no server required). For each keyword in
`keywords.txt` it queries the NewsData.io `/latest` endpoint, takes the top 10
results, de-duplicates, and appends new rows to the current week's CSV. Your AI
tool then consumes the weekly CSV for Stage 2 analysis.

## What it does

- Reads `keywords.txt` (one term/phrase per line).
- For each keyword: `GET https://newsdata.io/api/1/latest?q=<keyword>&language=en&country=us,ca&sort=relevancy&timeframe=24`
  and keeps the first 10 articles.
  - Multi-word keywords are automatically wrapped in quotes for **exact-phrase**
    matching. Unquoted, NewsData ORs the individual words together, which was
    the main source of irrelevant matches in early testing (`forest products`
    matching anything containing just "forest" or just "products").
  - A line prefixed `title:` searches `qInTitle` instead of `q` — the term must
    be in the headline, not just anywhere in the article. Much higher precision
    for short/generic terms; see the note at the top of `keywords.txt`.
  - `sort=relevancy` and `timeframe=24` (last 24 hours) are requested by
    default. Neither is confirmed to be free-tier-enabled — if the API rejects
    one, the script disables it for the rest of that run and logs a warning
    instead of silently failing every remaining keyword.
- **De-duplication:** one row per `(keyword, article_id)`, **plus** a second
  check on normalized title per keyword — this catches the same story
  syndicated across multiple domains (common with press-release / stock-news
  mills), which get a different `article_id` per domain but identical text.
  Articles NewsData itself flags `duplicate: true` are also skipped. An
  article that matches 3 *different* keywords still produces 3 rows; the same
  article reappearing under the same keyword on a later day is **not**
  re-added. `first_seen_date` records when we first saw it.
- **Weekly files:** all of a week's rows go in one CSV named
  `newsdata_<Monday>_to_<Sunday>.csv` (ISO dates), e.g.
  `newsdata_2026-09-01_to_2026-09-07.csv`. The first run of a new week creates
  the next file automatically. The file is **appended to** each day — never a new
  file per day.
- **Retention:** after writing, any weekly CSV whose week *ended* more than
  `prune_weeks` (default 12) weeks ago is deleted.

### Columns

`first_seen_date, keyword, pubDate, title, description, source_id, source_name,
link, category, country, language, article_id, image_url`

`description` is truncated to `summary_max_chars` (default 300, `0` disables
it) — cut on a word boundary where possible, with an ellipsis included in the
count so it never exceeds the limit. Applied last, after filtering/dedup, so
the content filter always sees the full untruncated text. Only affects rows
written going forward; existing rows in already-collected weeks are untouched
unless you re-run them through a script that rewrites `description`.

## Setup

Requires Python 3.11+ (uses the stdlib `tomllib`). `requests` is the only
third-party dependency.

```bash
cd "newsdata-fetch"
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
chmod 600 .env
# edit .env and paste your NewsData.io free-tier API key
```

Put your 80 keywords in `keywords.txt` (replace the examples).

## Run

```bash
source .venv/bin/activate
python3 fetch_news.py
```

With 80 keywords and the default 32-second throttle the run takes ~42 minutes
(the throttle keeps you under the free tier's 30-requests / 15-minutes limit).
Progress prints per keyword. Safe to run more than once a day — dedup means
re-runs only add genuinely new articles.

## Aggregator link resolution (Google News, NewsBreak)

Some sources give a link to their own aggregator page instead of the real
publisher URL. Both are resolved automatically, through one shared, small
mechanism (`resolve_link()` dispatches by host; add a new one by pairing an
`is_x_link()` + `resolve_x_link()`, no other plumbing needed):

- **Google News** (`news.google.com/rss/articles/...`) is an opaque, signed
  redirect token, not a working page. Resolved via the `googlenewsdecoder`
  package, which replicates Google's unofficial, undocumented internal
  decoding endpoint (one extra request per article).
- **NewsBreak** (`newsbreak.com/news/...`) is a real, working preview page
  that embeds the true source URL inline as `"originalUrl":"..."` in the
  page's own JSON. Resolved with a plain page fetch + regex — no extra
  dependency.

Both share one on-disk cache (`output/.resolved_link_cache.json`, so a link is
never re-resolved once seen) and only run on articles that survive dedup
(keeps aggregator request volume to a minimum). Any failure — package
missing, network error, page structure changed — just leaves the original
aggregator link in place; it still works as a landing page, so nothing
breaks. A one-line summary (`resolved / cache_hit / failed / errors`) prints
at the end of each run. Since both ride on undocumented mechanics, expect
occasional breakage if Google or NewsBreak change something (a
`pip install --upgrade googlenewsdecoder` covers the Google News side).
Disable either independently with `resolve_google_news_links = false` /
`resolve_newsbreak_links = false` in `config.toml`.

**When a link is resolved, `source_name` is updated to the real outlet**
(derived from the resolved URL's domain, e.g. `scienceblog.com` -> `Scienceblog`);
`source_id` is deliberately left as `newsbreak` / `google_news` so you can
still filter/track articles by their original aggregator. The domain-to-name
derivation is a simple heuristic (no public-suffix-list dependency), so it
mishandles two-part TLDs like `.co.uk` or `.com.au` (e.g. would yield "Co"
instead of the real second-level name) - fine for the `.com`/`.ca`/`.org`-style
domains seen so far; worth revisiting with a proper TLD list if `.co.uk`-style
sources start showing up in practice.

## Content filter

Before a row is ever written, `filter_reason()` drops:

- **Investor/stock-comparison syndication** — an explicit publisher/domain
  blocklist (MarketBeat network, Zacks, Motley Fool, Benzinga, etc.) plus a
  securities-language title regex (`shares of`, `NASDAQ:`, `price target`,
  `Q3 earnings`, ...) as a backstop for non-blocklisted domains.
- **Obituaries.**
- **Off-topic "forest"/"timber" homonyms** — Nottingham Forest FC, recipes,
  gardening, tourism, "forest bathing" wellness content, etc.
- **Anything with no genuine forestry/wood-industry term** anywhere in
  title+description (`require_industry_anchor`) — the broadest rule; catches
  whatever the other three miss.

Rejected articles never touch the CSV. A per-keyword `filtered` count and an
end-of-run reason breakdown print to the log. Toggle the whole thing off with
`content_filter_enabled = false`, or just the broad anchor rule with
`require_industry_anchor = false`, in `config.toml`.

**`retrofilter.py`** re-applies the current filter to already-collected
weekly CSVs — useful after tuning a rule, since `fetch_news.py` only filters
going forward. Backs up each file it changes to `output/backups/` first.

```bash
python3 retrofilter.py                 # all weekly CSVs in output/
python3 retrofilter.py output/newsdata_2026-09-07_to_2026-09-13.csv   # just one
```

## Configuration — `config.toml`

| Key | Default | Notes |
|---|---|---|
| `language` | `en` | `""` = no filter |
| `country` | `us,ca` | comma-separated ISO codes; `""` = no filter |
| `category` | *(empty)* | no filter — Forestry keywords span many categories |
| `sort` | `relevancy` | not confirmed free-tier-enabled; auto-disabled on rejection |
| `timeframe_hours` | `24` | last N hours (1-48); not confirmed free-tier-enabled; auto-disabled on rejection |
| `priority_domain` | *(empty)* | `"top"`/`"medium"`/`"low"` — experiment against syndication-mill noise; not confirmed free-tier-enabled |
| `results_per_keyword` | `10` | do not raise on the free tier (it returns 10/request) |
| `throttle_seconds` | `32` | keep >= 30 on the free tier |
| `prune_weeks` | `12` | weekly CSVs older than this are deleted |
| `request_timeout` / `max_retries` / `retry_backoff` | `30` / `5` / `5` | mostly for 429 handling |

## Scheduling — GitHub Actions

Deployed at `.github/workflows/fetch.yml` on the public repo
[`stature/newsdata-fetch`](https://github.com/stature/newsdata-fetch). No
server, no always-on machine required — this is the sole runner (the earlier
local macOS `launchd` agent was retired to avoid two independent processes
maintaining diverging dedup state / CSVs).

- **Trigger:** `schedule: cron: "0 16 * * *"` (16:00 UTC = noon EDT) plus
  `workflow_dispatch` for on-demand manual runs from the Actions tab or
  `gh workflow run fetch.yml`. GitHub Actions cron is always UTC and doesn't
  follow DST, so this drifts to ~11:00 AM EST roughly early Nov–mid March
  (about a 1-hour shift, twice a year) — edit the cron hour in the offseason
  if that matters; not automated, to keep this simple.
- **Secret:** `NEWSDATA_API_KEY`, stored as a GitHub Actions repo secret
  (Settings → Secrets and variables → Actions), injected as an env var for
  the run step. Never appears in logs or in the repo.
- **Output:** the job runs `fetch_news.py` exactly as locally, then commits
  any changed `output/*.csv` and `output/.resolved_link_cache.json` back to
  `main` as `github-actions[bot]`, and pushes. That commit is also what keeps
  GitHub from auto-disabling the schedule after 60 days of repo inactivity.
- **Public repo** was chosen so Actions minutes are unlimited/free (a private
  repo's free-tier 2,000 min/month would be a tight fit against a ~45-70 min
  daily run). Content is just headlines/links/keywords — no secrets, since
  those live in Secrets regardless of repo visibility.
- **Concurrency:** `group: newsdata-fetch, cancel-in-progress: false` — a
  manual trigger queues rather than colliding with a scheduled run in
  progress.

Validated locally with `actionlint .github/workflows/fetch.yml` before every
push that touches the workflow — same "validate before you risk it" habit as
the Caddy config work.

CORS does **not** affect any of this — CORS is a browser-only mechanism, and
this is a server-side script running on a GitHub-hosted runner. NewsData's
"CORS enabled for localhost only" on the free tier only blocks calling their
API from front-end browser JavaScript.

## Free-tier limits this job respects

- 200 credits/day, 10 articles/credit → 80 keywords = 80 credits/day.
- Rate limit 30 credits / 15 min → handled by `throttle_seconds` + 429 backoff.
- `q` capped at 100 characters → the script warns on any over-length keyword.
- Articles are ~12 h delayed and snippet-only (no full `content`) on free.

## Known limitation — relevance sorting may not apply on the free tier

`sort=relevancy` is requested by default, but it's unconfirmed whether the
free tier honors it — if the API rejects the param the script falls back to
NewsData's default (newest-first) and logs it. Check the run output for a
"disabled for this run" note. If relevance sorting turns out to be paid-tier
only, `title:` keywords (this file already uses it on the noisiest terms) are
the next-best lever, since requiring the term in the headline is itself a
strong relevance signal.

## Tuning noisy keywords

If a keyword is still pulling irrelevant results after phrase-quoting:
1. Try prefixing it `title:` in `keywords.txt` (requires the term in the
   headline — see the file's own comments for examples already applied).
2. If it's still noisy, it's likely too generic for this API's matching
   (e.g. single dictionary words like "biomass" or "timber" overlap with
   finance/stock-comparison boilerplate that happens to mention the word).
   Consider a more specific phrase, or plan to filter it further downstream
   in your AI analysis step instead of at collection time.
