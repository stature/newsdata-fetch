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

## Scheduling (later)

Nothing here is scheduled yet — run it by hand until the process is proven.
Once it is, options:

- **macOS `launchd`** — a `StartCalendarInterval` agent. Only fires when the Mac
  is awake, so fine for a workstation that's on during the day, not for
  guaranteed overnight runs.
- **cron on a server** — the Oracle VM once it exists, or any always-on box:
  `0 6 * * * cd /path/to/newsdata-fetch && .venv/bin/python fetch_news.py >> run.log 2>&1`
- **GitHub Actions** — a scheduled workflow that runs the script and commits the
  weekly CSV back to the repo (un-ignore `output/` first). Serverless; note
  scheduled Actions can start a few minutes late and pause after 60 days of repo
  inactivity.

CORS does **not** affect any of these — CORS is a browser-only mechanism, and
this is a server-side script. NewsData's "CORS enabled for localhost only" on the
free tier only blocks calling their API from front-end browser JavaScript.

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
