# newsdata-fetch — Stage 1 Forestry news collector

A small scheduled batch job (no MCP, no server required). For each keyword in
`keywords.txt` it queries the NewsData.io `/latest` endpoint, takes the top 10
results, and merges into the current week's CSV: one row per unique article,
with every matching keyword combined into that row. Your AI tool then
consumes the weekly CSV for Stage 2 analysis.

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
- **De-duplication:** one row per *unique article* — identity is `article_id`,
  falling back to normalized title (catches the same story syndicated across
  multiple domains, which get a different `article_id` per domain but
  identical text). An article matching 3 different keywords gets **one row**
  with `keyword` = `"kw1, kw2, kw3"`, not 3 separate rows — a keyword is added
  to that row's list the first time it matches; matching again later is a
  no-op. Articles NewsData itself flags `duplicate: true` are also skipped.
  `first_seen_date` is the earliest date any keyword matched it, and never
  changes once set.
- **Weekly files:** all of a week's data lives in one CSV named
  `newsdata_<Monday>_to_<Sunday>.csv` (ISO dates), e.g.
  `newsdata_2026-09-01_to_2026-09-07.csv`. The first run of a new week creates
  the next file automatically. Each run rewrites the file in place (existing
  rows may gain a merged keyword; genuinely new articles are added) — written
  atomically via a temp file + rename, so a crash mid-write can never leave a
  half-written CSV.
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

## Aggregator link resolution (Google News, NewsBreak, Bundle)

Some sources give a link to their own aggregator page instead of the real
publisher URL. All three are resolved automatically, through one shared,
small mechanism (`resolve_link()` dispatches by host; add a new one by
pairing an `is_x_link()` + `resolve_x_link()`, no other plumbing needed):

- **Google News** (`news.google.com/rss/articles/...`) is an opaque, signed
  redirect token, not a working page. Resolved via the `googlenewsdecoder`
  package, which replicates Google's unofficial, undocumented internal
  decoding endpoint (one extra request per article).
- **NewsBreak** (`newsbreak.com/news/...`) is a real, working preview page
  that embeds the true source URL inline as `"originalUrl":"..."` in the
  page's own JSON. Resolved with a plain page fetch + regex — no extra
  dependency.
- **Bundle** (`bundle.app/...`, `source_id` = `bundle_app`) is a curation app
  — each article page embeds the real source next to a `"shorter_link"`
  field in an inline JSON blob (shown to readers as a "Read More: {link}"
  anchor). Same plain fetch + regex approach, no extra dependency. Its page
  inconsistently renders that JSON with backslash-escaped quotes depending on
  where it lands in Next.js's streamed response, so the resolver normalizes
  that before matching — confirmed necessary by testing the same URL several
  times in a row and seeing both forms.

All three route their captured value through `_json_string_unescape()` rather
than a plain string replace — a regex-captured JSON string can contain
`\uXXXX` escapes (e.g. `&` for `&` in a tracking-parameter URL), and only
unescaping `\/` left those literal 6-character sequences in the URL, silently
producing a broken link. `_json_string_unescape()` wraps the capture in
quotes and lets Python's own JSON decoder handle every standard escape.

All three share one on-disk cache (`output/.resolved_link_cache.json`, so a
link is never re-resolved once seen) and only run on articles that survive
dedup (keeps aggregator request volume to a minimum). Any failure — package
missing, network error, page structure changed — just leaves the original
aggregator link in place; it still works as a landing page, so nothing
breaks. A one-line summary (`resolved / cache_hit / failed / errors`) prints
at the end of each run. Since all three ride on undocumented mechanics,
expect occasional breakage if a site changes something (a
`pip install --upgrade googlenewsdecoder` covers the Google News side).
Disable any independently with `resolve_google_news_links = false` /
`resolve_newsbreak_links = false` / `resolve_bundle_app_links = false` in
`config.toml`.

**When a link is resolved, `source_name` is updated to the real outlet**
(derived from the resolved URL's domain, e.g. `scienceblog.com` -> `Scienceblog`);
`source_id` is deliberately left as `newsbreak` / `google_news` / `bundle_app`
so you can still filter/track articles by their original aggregator. The
domain-to-name derivation is a simple heuristic (no public-suffix-list
dependency), so it mishandles two-part TLDs like `.co.uk` or `.com.au` (e.g.
would yield "Co" instead of the real second-level name) - fine for the
`.com`/`.ca`/`.org`-style domains seen so far; worth revisiting with a proper
TLD list if `.co.uk`-style sources start showing up in practice.

## Content filter

Before a row is ever written, `filter_reason()` drops:

- **Explicit source blocklist** (`blocked_sources` in `config.toml`) — add
  any `source_id`, `source_name` (as it appears in the CSV), or bare domain
  to drop everything from it, no code changes needed. Matched
  case-insensitively against all three independently. Currently blocks
  `prsync` (press-release market-report spam).
- **Investor/stock-comparison syndication** — an explicit publisher/domain
  blocklist (MarketBeat network, Zacks, Motley Fool, Benzinga, etc.) plus a
  securities-language title regex (`shares of`, `NASDAQ:`, `price target`,
  `Q3 earnings`, ...) as a backstop for non-blocklisted domains.
- **Obituaries** — checked against title **and** description (a local-paper
  obituary's headline is often just a name, e.g. "Clarence R. McCool Sr.";
  the giveaway language is in the body).
- **Off-topic "forest"/"timber"/"logging" homonyms** — Nottingham Forest FC,
  recipes, gardening, tourism, "forest bathing" wellness content, sports
  idioms ("logging a sack"), tech/privacy stories ("logging audio", "data
  logging"), etc. "logging" is a generic English word for "recording" far
  more often than it means the timber industry, so it's treated like
  "forest"/"timber" - see "Tuning noisy keywords" below.
- **Anything with no genuine forestry/wood-industry term** anywhere in
  title+description (`require_industry_anchor`) — the broadest rule; catches
  whatever the others miss.

**A keyword that's also a literal token inside `INDUSTRY_ANCHOR_RE` gets a
free pass through the anchor check** - the word that matched the search is
often the exact word the anchor rule is looking for, so it can't provide any
real protection against that keyword's own false positives (this is how the
"logging" homonyms above got through in the first place: `logging` is both
a searched keyword and an anchor term). Known overlap includes at least
`timber`, `sawmill`, `biomass`, `bioenergy`, `pulp`, `forestry`, `plywood`.
Worth checking any new single-word keyword against `INDUSTRY_ANCHOR_RE`
before relying on the anchor filter to catch its false positives - the fix
for a genuinely ambiguous one is usually to drop the bare form in favor of a
more specific phrase (as done for "logging") or add targeted `OFFTOPIC_RE`
patterns, not to expect the anchor rule to save it.

Rejected articles never touch the CSV. A per-keyword `filtered` count and an
end-of-run reason breakdown print to the log (`blocked_source` is its own
reason code, kept separate from `financial_reporting` for a clearer audit
trail). Toggle the whole thing off with `content_filter_enabled = false`, or
just the broad anchor rule with `require_industry_anchor = false`, in
`config.toml`.

**`retrofilter.py`** brings already-collected weekly CSVs up to what a fresh
run would produce today, with no NewsData API calls: re-applies the current
content filter (useful after tuning a rule or adding to `blocked_sources`),
resolves any still-outstanding aggregator links, truncates descriptions, and
merges any duplicate rows left over from before keyword-merging existed.
Backs up each file it changes to `output/backups/` first (never clobbers an
existing same-day backup - adds a numeric suffix).

```bash
python3 retrofilter.py                 # all weekly CSVs in output/
python3 retrofilter.py output/newsdata_2026-09-07_to_2026-09-13.csv   # just one
```

**Re-running the filter against an already-truncated description is handled
carefully, but only where it actually needs to be.** `filter_reason()` takes
a `description_is_complete` flag; `retrofilter.py` sets it `False` for any
row whose description already ends in the truncation ellipsis. This flag
gates **only** the industry-anchor check — a "must find a qualifying term,
else reject" rule, where missing text should be treated leniently, since the
term that justified keeping the article could be sitting in the
truncated-away tail. Re-checking it against truncated text risks wrongly
rejecting a good article on a second pass; this was caught by running
`retrofilter.py` twice in a row on the same file and seeing 2 previously-kept
articles wrongly rejected the second time, purely because their own earlier
truncation had cut off the text the anchor check depended on.

Obituary and off-topic checks are the opposite shape — "reject if this
disqualifying text is found" — and **always** check the description exactly
as stored, truncated or not, regardless of this flag: a genuine match in
text that's actually present is always a true positive, whatever got
trimmed elsewhere. The only risk there is a false negative (disqualifying
evidence that got truncated away, so a bad article isn't caught) - much more
tolerable than wrongly discarding good content, and no different in kind
from the gap that exists for *any* rule added after a row was already
written. Source- and title-only rules (blocklist, investor publishers,
securities language) are unaffected by description truncation either way and
always run. `fetch_news.py`'s own live run never hits any of this — it
always filters the fresh, full-length API response before any truncation
happens.

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
   Note this doesn't help when the word itself is ambiguous even in a
   headline (see step 3).
2. Check whether the keyword is also a literal token inside
   `INDUSTRY_ANCHOR_RE` (see the Content filter section above) — if so, the
   anchor filter provides no real protection against that keyword's own
   false positives, since the matched word satisfies its own anchor check.
3. If the word is generic enough to mean something else entirely in common
   English (e.g. "logging" as in "logging into an account" or "logging a
   stat" - removed as a bare keyword for exactly this reason, keeping only
   `logging industry`/`logging truck`), drop the bare form and keep only
   more specific phrases. Add targeted patterns to `OFFTOPIC_RE` as a
   backstop for the cases a specific phrase still lets through.
4. Otherwise it's likely just too generic for this API's matching (e.g.
   single dictionary words that overlap with finance/stock-comparison
   boilerplate happening to mention the word). Consider a more specific
   phrase, or plan to filter it further downstream in your AI analysis step
   instead of at collection time.
