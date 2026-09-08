#!/usr/bin/env python3
"""
Stage 1 - daily Forestry news collector.

For each keyword in keywords.txt, query the NewsData.io /latest endpoint,
take the top 10 results, de-duplicate against the current week's CSV
(one row per keyword + article, keyed by both article_id AND normalized
title, since syndicated republishes get a fresh article_id per domain),
append new rows, and prune weekly CSV files older than the configured
retention window.

Config lives in config.toml; the API key lives in .env (NEWSDATA_API_KEY).
Run:  python3 fetch_news.py
"""

import csv
import datetime
import json
import os
import pathlib
import re
import sys
import time
import tomllib
from urllib.parse import urlsplit

import requests

try:
    from googlenewsdecoder import gnewsdecoder
except ImportError:
    gnewsdecoder = None  # resolution silently skipped; see resolve_google_news_link()

BASE_URL = "https://newsdata.io/api/1/latest"  # legacy alias: /api/1/news
GOOGLE_NEWS_HOST = "news.google.com"
NEWSBREAK_HOST = "newsbreak.com"
NEWSBREAK_ORIGINAL_URL_RE = re.compile(r'"originalUrl"\s*:\s*"([^"]+)"')
BUNDLE_APP_HOST = "bundle.app"
# Bundle embeds the true source right next to a "shorter_link" (bare domain)
# field in an inline JSON blob - anchoring on that pair avoids matching some
# unrelated "link" key elsewhere on the page.
BUNDLE_APP_LINK_RE = re.compile(r'"shorter_link"\s*:\s*"[^"]*"\s*,\s*"link"\s*:\s*"([^"]+)"')
LINK_CACHE_FILENAME = ".resolved_link_cache.json"
SCRIPT_DIR = pathlib.Path(__file__).resolve().parent

COLUMNS = [
    "first_seen_date",
    "keyword",
    "pubDate",
    "title",
    "description",
    "source_id",
    "source_name",
    "link",
    "category",
    "country",
    "language",
    "article_id",
    "image_url",
]


def load_env(path: pathlib.Path) -> None:
    """Minimal .env loader - KEY=VALUE lines, # comments. Real env vars win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        val = val.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), val)


def load_config(path: pathlib.Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def week_bounds(today: datetime.date):
    """Monday .. Sunday of the week containing `today`."""
    monday = today - datetime.timedelta(days=today.weekday())
    return monday, monday + datetime.timedelta(days=6)


def weekly_path(output_dir: pathlib.Path, monday, sunday) -> pathlib.Path:
    return output_dir / f"newsdata_{monday.isoformat()}_to_{sunday.isoformat()}.csv"


def normalize_title(title: str) -> str:
    """Collapse a title to a loose fingerprint so syndicated republishes
    (same story, different domain, different article_id) dedup together."""
    t = (title or "").lower()
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return t.strip()


def load_existing_keys(csv_path: pathlib.Path):
    """Returns (seen_ids, seen_titles) - both sets of (keyword, value) pairs."""
    seen_ids, seen_titles = set(), set()
    if not csv_path.exists():
        return seen_ids, seen_titles
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            kw = row.get("keyword", "")
            seen_ids.add((kw, row.get("article_id", "")))
            seen_titles.add((kw, normalize_title(row.get("title", ""))))
    return seen_ids, seen_titles


def read_keywords(path: pathlib.Path) -> list:
    """Each line is either a plain keyword/phrase, or 'title:<keyword>' to
    search qInTitle instead of q (much higher precision for short/ambiguous
    terms - the term must appear in the headline, not just anywhere in the
    article). Returns a list of (text, mode) tuples, mode in {"q", "title"}."""
    out = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        if ln.lower().startswith("title:"):
            out.append((ln[len("title:"):].strip(), "title"))
        else:
            out.append((ln, "q"))
    return out


def prune(output_dir: pathlib.Path, prune_weeks: int, today: datetime.date) -> None:
    cutoff = today - datetime.timedelta(weeks=prune_weeks)
    for p in sorted(output_dir.glob("newsdata_*_to_*.csv")):
        try:
            end = datetime.date.fromisoformat(p.stem.split("_to_")[1])
        except (IndexError, ValueError):
            continue
        if end < cutoff:
            print(f"  prune: {p.name} (week ended {end}, older than {prune_weeks} weeks)")
            p.unlink()


def is_google_news_link(link: str) -> bool:
    return GOOGLE_NEWS_HOST in (link or "")


def is_newsbreak_link(link: str) -> bool:
    return NEWSBREAK_HOST in (link or "")


def is_bundle_app_link(link: str) -> bool:
    return BUNDLE_APP_HOST in (link or "")


def load_link_cache(path: pathlib.Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_link_cache(path: pathlib.Path, cache: dict) -> None:
    try:
        path.write_text(json.dumps(cache), encoding="utf-8")
    except OSError as e:
        print(f"WARNING: could not write link cache: {e}")


def resolve_google_news_link(link: str, cfg: dict, cache: dict, stats: dict) -> str:
    """Google News RSS links are opaque redirect tokens, not the real article
    URL. This decodes them via the (unofficial, reverse-engineered) mechanism
    the `googlenewsdecoder` package implements. Best-effort: any failure just
    keeps the original Google News link, never breaks the run."""
    if link in cache:
        stats["cache_hit"] += 1
        return cache[link] or link
    if gnewsdecoder is None:
        return link  # package not installed; see requirements.txt

    try:
        result = gnewsdecoder(link, interval=cfg.get("google_news_decode_interval", 1))
    except Exception as e:  # library wraps an unofficial endpoint - anything can happen
        stats["errors"] += 1
        cache[link] = None
        print(f"    Google News link decode error, keeping redirect link: {e}")
        return link

    if result and result.get("status") and result.get("decoded_url"):
        cache[link] = result["decoded_url"]
        stats["resolved"] += 1
        return result["decoded_url"]

    stats["failed"] += 1
    cache[link] = None
    return link


def _json_string_unescape(raw: str) -> str:
    """Decode standard JSON string escapes (\\uXXXX, \\/, \\", etc.) from a
    regex-captured JSON string body - e.g. a URL's "&" surviving as the
    literal 6 characters "\\u0026" if only \\/ were unescaped. Falls back to
    a minimal manual unescape if the capture isn't valid JSON on its own."""
    try:
        return json.loads(f'"{raw}"')
    except (json.JSONDecodeError, ValueError):
        return raw.replace("\\/", "/")


def resolve_newsbreak_link(link: str, cfg: dict, session, cache: dict, stats: dict) -> str:
    """NewsBreak's page (unlike Google News) isn't a redirect - it's a working
    preview page that embeds the real source URL as "originalUrl" in an
    inline JSON blob. No decoding library needed, just one GET + a regex.
    Best-effort: any failure just keeps the NewsBreak link, which still works
    as a landing page, so it's a safe fallback."""
    if link in cache:
        stats["cache_hit"] += 1
        return cache[link] or link

    try:
        resp = session.get(link, timeout=cfg.get("request_timeout", 30))
        resp.raise_for_status()
        match = NEWSBREAK_ORIGINAL_URL_RE.search(resp.text)
    except Exception as e:
        stats["errors"] += 1
        cache[link] = None
        print(f"    NewsBreak link resolve error, keeping NewsBreak link: {e}")
        return link

    if match:
        original = _json_string_unescape(match.group(1))
        cache[link] = original
        stats["resolved"] += 1
        return original

    stats["failed"] += 1
    cache[link] = None
    return link


def resolve_bundle_app_link(link: str, cfg: dict, session, cache: dict, stats: dict) -> str:
    """Bundle (bundle_app) is a curation app - each article is a working page
    that embeds the true source next to a "shorter_link" field in an inline
    JSON blob (also shown to readers as a "Read More: {link}" anchor). Same
    approach as NewsBreak: one GET + a regex, no decoding library needed.
    Best-effort: any failure just keeps the Bundle link, which still works as
    a landing page, so it's a safe fallback."""
    if link in cache:
        stats["cache_hit"] += 1
        return cache[link] or link

    try:
        resp = session.get(link, timeout=cfg.get("request_timeout", 30))
        resp.raise_for_status()
        # Bundle's Next.js page sometimes renders this JSON blob with its
        # quotes backslash-escaped (nested one level deeper in the RSC
        # stream) and sometimes not, inconsistently between requests for the
        # same article. Normalize before matching so either form works.
        text = resp.text.replace('\\"', '"')
        match = BUNDLE_APP_LINK_RE.search(text)
    except Exception as e:
        stats["errors"] += 1
        cache[link] = None
        print(f"    Bundle link resolve error, keeping Bundle link: {e}")
        return link

    if match:
        original = _json_string_unescape(match.group(1))
        cache[link] = original
        stats["resolved"] += 1
        return original

    stats["failed"] += 1
    cache[link] = None
    return link


def _hostname(url: str) -> str:
    """Lowercase hostname with a leading www. stripped, or "" on anything
    unparseable. Shared by the source-name derivation and the content filter's
    investor-domain check."""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def domain_to_source_name(url: str) -> str:
    """Best-effort friendly label for the real publisher, derived from the
    resolved URL's domain - e.g. https://www.scienceblog.com/... -> "Scienceblog".
    Not full public-suffix-list handling, just good enough for a source label
    (matches the rough style of NewsData's own source_name values)."""
    host = _hostname(url)
    if not host:
        return ""
    parts = host.split(".")
    label = parts[-2] if len(parts) >= 2 else parts[0]
    label = re.sub(r"[^a-z0-9]+", " ", label).strip()
    return label.title() if label else host


def resolve_link(link: str, cfg: dict, session, cache: dict, stats: dict) -> str:
    """Dispatches to the right resolver for known aggregator hosts; anything
    else passes through untouched. Add a new host here (is_x_link + resolve_x)
    to extend - no other plumbing needed."""
    if is_google_news_link(link):
        if not cfg.get("resolve_google_news_links", True) or gnewsdecoder is None:
            return link
        return resolve_google_news_link(link, cfg, cache, stats)
    if is_newsbreak_link(link):
        if not cfg.get("resolve_newsbreak_links", True):
            return link
        return resolve_newsbreak_link(link, cfg, session, cache, stats)
    if is_bundle_app_link(link):
        if not cfg.get("resolve_bundle_app_links", True):
            return link
        return resolve_bundle_app_link(link, cfg, session, cache, stats)
    return link


# ---------------------------------------------------------------------------
# Deterministic content filter - ported from a reference script the user
# supplied (a Stage 0 pre-filter from a separate pipeline). Applied before an
# article is ever written to the CSV, so rejected content never lands in the
# output at all. Rule order is fixed - first match wins - so behavior stays
# predictable. Only the rule families that add value here were ported;
# recency-window and global-duplicate rules were dropped as redundant with
# the weekly file boundary and the existing per-keyword dedup.
# ---------------------------------------------------------------------------

INVESTOR_PUBLISHERS = {
    # MarketBeat syndication network - the dominant noise source in this feed
    "marketbeat", "ticker report", "tickerreport", "the lincolnian online",
    "thelincolnianonline", "daily political", "dailypolitical", "markets daily",
    "the markets daily", "themarketsdaily", "americanbankingnews",
    "american banking news", "baseball news source", "baseballnewssource",
    "bbns", "zolmax", "the cerbat gem", "cerbat gem", "com unicaciones",
    "insider trading", "insidertrading", "etf daily news", "defense world",
    "modern readers", "tech know bits", "watch list news", "wkrb news",
    "the ledger gazette", "dispatch tribunal", "sports perspectives",
    "enterprise leader", "transcript daily", "mayfield recorder",
    # other investor-content shops
    "zacks", "zacks investment research", "simply wall st", "insider monkey",
    "insidermonkey", "motley fool", "the motley fool", "tipranks", "benzinga",
    "stocktitan", "gurufocus", "seeking alpha", "investing.com", "invezz",
    "stocktwits", "24/7 wall st", "247 wall st", "smarteranalyst",
}

INVESTOR_DOMAINS = {
    "marketbeat.com", "tickerreport.com", "thelincolnianonline.com",
    "dailypolitical.com", "themarketsdaily.com", "americanbankingnews.com",
    "baseballnewssource.com", "zolmax.com", "thecerbatgem.com",
    "etfdailynews.com", "defenseworld.net", "zacks.com", "simplywall.st",
    "insidermonkey.com", "fool.com", "tipranks.com", "benzinga.com",
    "stocktitan.net", "gurufocus.com", "seekingalpha.com", "investing.com",
    "modernreaders.com", "watchlistnews.com", "wkrb13.com",
}

SECURITIES_TITLE_RE = re.compile(r"""(
    \bshares?\ of\b | \bstock(s)?\b | \bNASDAQ:?\b | \bNYSE:?\b | \bOTCMKTS:?\b |
    \bTSX:?\b | \bLON:?\b | \bEPS\b | price\ target | \banalyst(s)?\b |
    \bdividend | short\ interest | holdings\ in | stake\ in | position\ in |
    \bvaluation\b | head[\s-]to[\s-]head | financial\ (analysis|review|comparison) |
    \bcontrasting\b | \bcomparing\b | \breviewing\b | \banalyzing\b |
    should\ you\ buy | stocks?\ to\ (consider|research|follow|watch) |
    \bQ[1-4]\ (results|earnings) | \bbuy\ rating\b | \bsell\ rating\b |
    \bupgraded?\ (to|by)\b | \bdowngraded?\b | market\ cap |
    52[\s-]week | \bshort\ seller | \b13[FDG]\b | insider\ (buying|selling) |
    \bbuyback\b | \bIPO\b | earnings\ (beat|miss|call|report)
)""", re.I | re.X)

OBITUARY_TITLE_RE = re.compile(r"""(
    \bdies\b | \bdied\b | dead\ at | passes\ away | passed\ away |
    \bobituary\b | in\ memoriam | \bmemorial\b | \bremembering\b |
    tribute\ to | \bfuneral\b | celebration\ of\ life | laid\ to\ rest |
    \ba\ life\ in\b | legacy\ of | \b(19|20)\d{2}\s*[-–]\s*(19|20)\d{2}\b |
    \bwas\ \d{2}\b
)""", re.I | re.X)

# Homonym / off-topic noise - the words that make "forest" and "timber"
# unusable as bare keywords against a general news index.
OFFTOPIC_RE = re.compile(r"""(
    nott(m|ingham)\ forest | \bforest\ green\b | jurrien\ timber |
    \bvs\.?\ | \bv\.\ | premier\ league | \bfc\b | \bafc\b | matchday |
    team\ news | \bfixture | \bkick[\s-]?off\b |
    \brecipe\b | cheesecake | \bcake\b | \bbaking\b | \bdessert\b |
    \bgardening\b | master\ gardener | \bhoneymoon\b | \btourism\b |
    \bmushroom\ tourism\b | \bfestival\b | chainsaw\ carv | wood\ carv |
    \b5k\b | obstacle\ course | \bmarathon\b |
    musical\ memoir | \bhonky\ tonk | \bmemories\b |
    black\ forest | forest\ bathing | \bticks?\ to\ your\b
)""", re.I | re.X)

# Category anchor: a surviving article must contain at least one genuine
# industry token somewhere in title+description. Recall net, not a judgment
# call - your AI analysis step still decides relevance for what passes.
INDUSTRY_ANCHOR_RE = re.compile(r"""(
    sawmill | saw\ mill | lumber | timber | logging | log\ (supply|truck|haul|price)
    | pulp | paper\ mill | containerboard | tissue\ mill | \bOSB\b | plywood
    | veneer | engineered\ wood | mass\ timber | cross[\s-]laminated | \bCLT\b
    | glulam | \bLVL\b | wood\ pellet | biomass | bioenergy | black\ liquor
    | wood\ (fiber|fibre|chip|product|construction|waste)
    | forest\ (management|service|products|health|plan|operations|thinning|restoration)
    | national\ forest | timberland | forestry | silvicultur | reforest
    | harvest | clearcut | cut\ block | stumpage | timber\ sale | salvage
    | wildfire | prescribed\ (burn|fire) | bark\ beetle | spruce\ budworm
    | softwood\ lumber | \blumber\ (duty|duties|tariffs?) | anti[\s-]dumping
    | housing\ start | homebuild | multifamily | repair\ and\ remodel
    | \bmill\ (closure|curtail|expansion|worker|job|shift|rebuild)
    | roadless | building\ code | \bprefab | modular\ (construction|housing)
    | log\ export | \bstumpage\b | woodland\ owner | \bpellet\ (plant|mill)
)""", re.I | re.X)


def filter_reason(art: dict, cfg: dict):
    """Returns a reason code string if `art` should be dropped, else None.
    Checked BEFORE aggregator link resolution, so the investor-domain check
    only fires for direct (non-aggregator) links - text-based rules are the
    backstop for investor content laundered through Google News/NewsBreak."""
    if not cfg.get("content_filter_enabled", True):
        return None

    title = art.get("title", "") or ""
    description = art.get("description", "") or ""
    blob = f"{title} || {description}"
    source_id = (art.get("source_id") or "").strip().lower()
    source_name = (art.get("source_name") or "").strip().lower()
    source = source_name or source_id
    domain = _hostname(art.get("link", ""))

    blocked = {s.strip().lower() for s in cfg.get("blocked_sources", []) if s.strip()}
    if blocked and (source_id in blocked or source_name in blocked or domain in blocked):
        return "blocked_source"
    if source in INVESTOR_PUBLISHERS or domain in INVESTOR_DOMAINS:
        return "financial_reporting"
    if SECURITIES_TITLE_RE.search(title):
        return "financial_reporting"
    if OBITUARY_TITLE_RE.search(title):
        return "obituary"
    if OFFTOPIC_RE.search(blob):
        return "offtopic"
    if cfg.get("require_industry_anchor", True) and not INDUSTRY_ANCHOR_RE.search(blob):
        return "no_industry_anchor"
    return None


def _joinlist(v) -> str:
    if isinstance(v, list):
        return ", ".join(str(x) for x in v if x)
    return v or ""


def build_params(api_key, keyword_text, mode, cfg, disabled_params) -> dict:
    field = "qInTitle" if mode == "title" else "q"
    # Exact-phrase match for multi-word keywords - unquoted, NewsData ORs the
    # individual words together, which is the main source of noisy matches.
    value = f'"{keyword_text}"' if " " in keyword_text else keyword_text

    params = {"apikey": api_key, field: value}
    if cfg.get("language"):
        params["language"] = cfg["language"]
    if cfg.get("country"):
        params["country"] = cfg["country"]
    if cfg.get("category"):
        params["category"] = cfg["category"]
    if cfg.get("priority_domain"):
        params["prioritydomain"] = cfg["priority_domain"]
    if cfg.get("sort") and "sort" not in disabled_params:
        params["sort"] = cfg["sort"]
    if cfg.get("timeframe_hours") and "timeframe" not in disabled_params:
        params["timeframe"] = cfg["timeframe_hours"]
    return params


def fetch_keyword(session, api_key, keyword_text, mode, cfg, disabled_params) -> list:
    backoff = cfg.get("retry_backoff", 5)
    timeout = cfg.get("request_timeout", 30)
    max_retries = cfg.get("max_retries", 5)

    for attempt in range(max_retries):
        params = build_params(api_key, keyword_text, mode, cfg, disabled_params)
        try:
            resp = session.get(BASE_URL, params=params, timeout=timeout)
        except requests.RequestException as e:
            wait = backoff * (2 ** attempt)
            print(f"    network error ({e}); retry in {wait}s")
            time.sleep(wait)
            continue

        if resp.status_code == 200:
            return resp.json().get("results") or []
        if resp.status_code == 429:
            wait = backoff * (2 ** attempt)
            print(f"    429 rate-limited; sleeping {wait}s")
            time.sleep(wait)
            continue
        if resp.status_code == 401:
            sys.exit("ERROR: 401 Unauthorized - check NEWSDATA_API_KEY in .env")
        if resp.status_code in (422, 400):
            msg = resp.text
            lower = msg.lower()
            # A param unsupported on this plan shouldn't kill every remaining
            # keyword - drop it tenant-wide and retry this one immediately.
            if "sort" in params and "sort" in lower:
                print(f"    'sort' rejected by API (plan restriction?) - disabling for rest of run: {msg[:150]}")
                disabled_params.add("sort")
                continue
            if "timeframe" in params and "timeframe" in lower:
                print(f"    'timeframe' rejected by API (plan restriction?) - disabling for rest of run: {msg[:150]}")
                disabled_params.add("timeframe")
                continue
            if "prioritydomain" in params and "domain" in lower:
                print(f"    'prioritydomain' rejected by API (plan restriction?) - disabling for rest of run: {msg[:150]}")
                disabled_params.add("priority_domain")
                continue
            print(f"    HTTP {resp.status_code} for {keyword_text!r}: {msg[:200]} - skipping")
            return []
        print(f"    HTTP {resp.status_code}: {resp.text[:200]}")
        resp.raise_for_status()

    print(f"    gave up on {keyword_text!r} after {max_retries} retries")
    return []


def truncate_text(text: str, max_chars: int) -> str:
    """Truncate to at most max_chars total (ellipsis included), breaking on a
    word boundary where that doesn't throw away too much of the budget."""
    text = text or ""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    ellipsis = "…"
    budget = max_chars - len(ellipsis)
    if budget <= 0:
        return text[:max_chars]
    cut = text[:budget]
    last_space = cut.rfind(" ")
    if last_space > budget * 0.6:
        cut = cut[:last_space]
    return cut.rstrip() + ellipsis


def to_row(keyword_text, art, today, cfg) -> dict:
    return {
        "first_seen_date": today.isoformat(),
        "keyword": keyword_text,
        "pubDate": art.get("pubDate", "") or "",
        "title": art.get("title", "") or "",
        "description": truncate_text(art.get("description", "") or "", cfg.get("summary_max_chars", 300)),
        "source_id": art.get("source_id", "") or "",
        "source_name": art.get("source_name", "") or art.get("source_id", "") or "",
        "link": art.get("link", "") or "",
        "category": _joinlist(art.get("category")),
        "country": _joinlist(art.get("country")),
        "language": art.get("language", "") or "",
        "article_id": art.get("article_id", "") or art.get("link", "") or "",
        "image_url": art.get("image_url", "") or "",
    }


def main() -> None:
    load_env(SCRIPT_DIR / ".env")
    cfg = load_config(SCRIPT_DIR / "config.toml")

    api_key = os.environ.get("NEWSDATA_API_KEY")
    if not api_key:
        sys.exit("ERROR: NEWSDATA_API_KEY not set - copy .env.example to .env and fill it in")

    output_dir = (SCRIPT_DIR / cfg.get("output_dir", "output")).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    kw_file = SCRIPT_DIR / cfg.get("keywords_file", "keywords.txt")
    keywords = read_keywords(kw_file)
    if not keywords:
        sys.exit(f"ERROR: no keywords in {kw_file}")
    for text, _mode in keywords:
        if len(text) > 100:
            print(f"WARNING: keyword exceeds free-tier 100-char q limit: {text!r}")

    today = datetime.date.today()
    monday, sunday = week_bounds(today)
    csv_path = weekly_path(output_dir, monday, sunday)
    is_new_file = not csv_path.exists()
    seen_ids, seen_titles = load_existing_keys(csv_path)

    print(f"Week {monday} .. {sunday}  ->  {csv_path.name}")
    print(f"{len(keywords)} keywords; {len(seen_ids)} (keyword, article) pairs already recorded this week")

    throttle = cfg.get("throttle_seconds", 32)
    per_kw = cfg.get("results_per_keyword", 10)
    disabled_params: set = set()

    if cfg.get("resolve_google_news_links", True) and gnewsdecoder is None:
        print("WARNING: resolve_google_news_links is on but 'googlenewsdecoder' isn't installed "
              "(pip install -r requirements.txt) - Google News links will stay as redirect URLs.")
    link_cache_path = output_dir / LINK_CACHE_FILENAME
    link_cache = load_link_cache(link_cache_path)
    link_stats = {"resolved": 0, "failed": 0, "errors": 0, "cache_hit": 0}

    session = requests.Session()
    session.headers.update({"User-Agent": "newsdata-fetch/1.0 (+stage1 forestry)"})

    filter_counts: dict = {}
    new_rows = []
    for i, (kw_text, mode) in enumerate(keywords, 1):
        label = f"title:{kw_text}" if mode == "title" else kw_text
        print(f"[{i}/{len(keywords)}] {label}")
        articles = fetch_keyword(session, api_key, kw_text, mode, cfg, disabled_params)[:per_kw]
        added, skipped_dupe, skipped_filtered = 0, 0, 0
        for art in articles:
            if art.get("duplicate"):
                skipped_dupe += 1
                continue
            reason = filter_reason(art, cfg)
            if reason:
                skipped_filtered += 1
                filter_counts[reason] = filter_counts.get(reason, 0) + 1
                continue
            aid = art.get("article_id") or art.get("link")
            if not aid:
                continue
            title_key = (kw_text, normalize_title(art.get("title", "")))
            id_key = (kw_text, aid)
            if id_key in seen_ids or title_key in seen_titles:
                skipped_dupe += 1
                continue
            seen_ids.add(id_key)
            seen_titles.add(title_key)
            # Only resolve links we're actually keeping - keeps aggregator
            # request volume to a minimum. No-ops for any other host.
            original_link = art.get("link", "")
            resolved = resolve_link(original_link, cfg, session, link_cache, link_stats)
            if resolved != original_link:
                art = dict(art)
                art["link"] = resolved
                # source_id intentionally left as-is ("newsbreak" / "google_news")
                # for tracking; source_name is updated to the real outlet.
                real_name = domain_to_source_name(resolved)
                if real_name:
                    art["source_name"] = real_name
            new_rows.append(to_row(kw_text, art, today, cfg))
            added += 1
        print(f"    {len(articles)} returned, {added} new, {skipped_dupe} duplicate/seen, {skipped_filtered} filtered")
        if i < len(keywords):
            time.sleep(throttle)

    save_link_cache(link_cache_path, link_cache)
    if any(link_stats.values()):
        print(f"Aggregator link resolution (Google News + NewsBreak): {link_stats['resolved']} resolved, "
              f"{link_stats['cache_hit']} from cache, {link_stats['failed']} failed, "
              f"{link_stats['errors']} errors (kept original link)")
    if filter_counts:
        breakdown = ", ".join(f"{reason}: {n}" for reason, n in sorted(filter_counts.items()))
        print(f"Content filter: {sum(filter_counts.values())} rejected ({breakdown})")

    if new_rows:
        with open(csv_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=COLUMNS)
            if is_new_file:
                writer.writeheader()
            writer.writerows(new_rows)
        print(f"Appended {len(new_rows)} new row(s) to {csv_path.name}")
    else:
        print("No new rows to append.")

    if disabled_params:
        print(f"Note: these params were rejected by the API and disabled for this run: {sorted(disabled_params)}")

    prune(output_dir, cfg.get("prune_weeks", 12), today)
    print("Done.")


if __name__ == "__main__":
    main()
