#!/usr/bin/env python3
"""
RSS side-collector - runs alongside fetch_news.py, writes a SEPARATE weekly
file: newsdata_<Mon>_to_<Sun>_RSS.csv, same columns.

Purpose: niche industry media / corporate blogs that NewsData.io doesn't
index but publish RSS/Atom feeds. These sources are curated but broad, so
this output is lower-trust than the keyword-driven NewsData file - it's
meant to be run through downstream AI relevance review, not consumed as-is.
Kept fully separate so it can be removed cleanly (delete this file +
feeds.txt, drop the rss_* keys from config.toml, drop feedparser from
requirements.txt, drop one step from the workflow - fetch_news.py never
imports from here).

Differences from the NewsData job:
  - every row's "keyword" column is the literal "(rss)"
  - the industry-anchor filter rule is OFF by default (rss_require_industry_
    anchor) - the other content-filter rules (blocked_sources, investor
    spam, obituaries, off-topic homonyms) still apply
  - de-dup is by article identity within the _RSS file (same as the main
    job), PLUS a cross-check against that week's NewsData file: anything
    already collected there is skipped here
  - first run of a new week backfills to Monday 00:00 UTC; every run after
    that only looks back rss_recency_hours (default 24)

Run:  python3 fetch_rss.py
"""

import csv
import datetime
import html
import os
import pathlib
import re
import sys
import time
from datetime import timezone
from urllib.parse import urlsplit

import feedparser
import requests

from fetch_news import (
    COLUMNS,
    LINK_CACHE_FILENAME,
    SCRIPT_DIR,
    _hostname,
    _joinlist,
    domain_to_source_name,
    filter_reason,
    load_config,
    load_env,
    load_existing_rows,
    load_link_cache,
    normalize_title,
    prune,
    resolve_link,
    save_link_cache,
    truncate_text,
    week_bounds,
)

RSS_SUFFIX = "_RSS"


def read_feeds(path: pathlib.Path) -> list:
    """One feed URL per line; blank lines and lines starting with # ignored."""
    if not path.exists():
        return []
    out = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            out.append(ln)
    return out


def strip_html(text: str) -> str:
    """RSS summaries frequently carry HTML tags and entities - flatten to
    plain text. Deliberately minimal (regex, not a parser), same approach as
    the NewsBreak/Bundle scrapers, to avoid another dependency."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def entry_datetime(entry) -> datetime.datetime | None:
    """feedparser normalizes published/updated to a UTC struct_time."""
    for key in ("published_parsed", "updated_parsed"):
        st = entry.get(key)
        if st:
            try:
                return datetime.datetime(*st[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                pass
    return None


def entry_image(entry) -> str:
    thumb = entry.get("media_thumbnail")
    if thumb and isinstance(thumb, list) and thumb[0].get("url"):
        return thumb[0]["url"]
    media = entry.get("media_content")
    if media and isinstance(media, list) and media[0].get("url"):
        return media[0]["url"]
    for enc in entry.get("enclosures", []) or []:
        if str(enc.get("type", "")).startswith("image/") and enc.get("href"):
            return enc["href"]
    return ""


def entry_description(entry) -> str:
    for key in ("summary", "description"):
        if entry.get(key):
            return strip_html(entry[key])
    content = entry.get("content")
    if content and isinstance(content, list) and content[0].get("value"):
        return strip_html(content[0]["value"])
    return ""


def entry_pubdate(entry) -> str:
    dt = entry_datetime(entry)
    if dt:
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    return str(entry.get("published") or entry.get("updated") or "")


def fetch_feed(session, url: str, cfg: dict):
    """Returns (feed_title, entries) or (None, []) on any failure - one bad
    feed must never take the run down."""
    try:
        resp = session.get(url, timeout=cfg.get("rss_feed_timeout", 30))
        resp.raise_for_status()
    except Exception as e:
        print(f"  feed error, skipping: {url}\n    {e}")
        return None, []
    parsed = feedparser.parse(resp.content)
    if parsed.bozo and not parsed.entries:
        print(f"  feed unparseable, skipping: {url}\n    {parsed.get('bozo_exception')}")
        return None, []
    feed_title = (parsed.feed.get("title") or "").strip()
    return feed_title, parsed.entries


def load_newsdata_identity(newsdata_csv: pathlib.Path):
    """Normalized titles + links already in this week's NewsData file, so we
    don't re-surface them in the RSS file."""
    titles, links = set(), set()
    if not newsdata_csv.exists():
        return titles, links
    with open(newsdata_csv, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            nt = normalize_title(row.get("title", ""))
            if nt:
                titles.add(nt)
            lk = (row.get("link") or "").strip()
            if lk:
                links.add(lk)
    return titles, links


def build_row(entry, feed_title, feed_url, today) -> dict:
    link = (entry.get("link") or "").strip()
    domain = _hostname(link) or _hostname(feed_url)
    return {
        "first_seen_date": today.isoformat(),
        "keyword": "(rss)",
        "pubDate": entry_pubdate(entry),
        "title": (entry.get("title") or "").strip(),
        "description": entry_description(entry),  # truncated by caller
        "source_id": domain,
        "source_name": feed_title or domain_to_source_name(link) or domain,
        "link": link,
        "category": _joinlist([t.get("term") for t in entry.get("tags", []) or [] if t.get("term")]),
        "country": "",
        "language": "",
        "article_id": (entry.get("id") or entry.get("guid") or link or "").strip(),
        "image_url": entry_image(entry),
    }


def main() -> None:
    load_env(SCRIPT_DIR / ".env")
    cfg = load_config(SCRIPT_DIR / "config.toml")

    if not cfg.get("rss_enabled", True):
        print("rss_enabled = false - nothing to do.")
        return

    output_dir = (SCRIPT_DIR / cfg.get("output_dir", "output")).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    feeds_file = SCRIPT_DIR / cfg.get("rss_feeds_file", "feeds.txt")
    feeds = read_feeds(feeds_file)
    if not feeds:
        print(f"No feeds in {feeds_file} - nothing to do.")
        return

    today = datetime.date.today()
    monday, sunday = week_bounds(today)
    rss_csv = output_dir / f"newsdata_{monday.isoformat()}_to_{sunday.isoformat()}{RSS_SUFFIX}.csv"
    newsdata_csv = output_dir / f"newsdata_{monday.isoformat()}_to_{sunday.isoformat()}.csv"

    first_run_this_week = not rss_csv.exists()
    if first_run_this_week:
        cutoff = datetime.datetime(monday.year, monday.month, monday.day, tzinfo=timezone.utc)
        print(f"First RSS run this week - backfilling entries since {cutoff.date()}")
    else:
        cutoff = datetime.datetime.now(timezone.utc) - datetime.timedelta(hours=cfg.get("rss_recency_hours", 24))
        print(f"Entries since {cutoff.isoformat(timespec='minutes')}")

    rows_in_order, by_id, by_title = load_existing_rows(rss_csv)
    cross_dedupe = cfg.get("rss_cross_dedupe_with_newsdata", True)
    nd_titles, nd_links = load_newsdata_identity(newsdata_csv) if cross_dedupe else (set(), set())

    # RSS uses the same content filter but with the anchor rule forced to the
    # rss_* setting (default off).
    eff_cfg = {**cfg, "require_industry_anchor": cfg.get("rss_require_industry_anchor", False)}

    print(f"Week {monday} .. {sunday}  ->  {rss_csv.name}")
    print(f"{len(feeds)} feeds; {len(rows_in_order)} article(s) already in this week's RSS file"
          f"{f'; {len(nd_titles)} in the NewsData file to skip' if cross_dedupe else ''}")

    link_cache_path = output_dir / LINK_CACHE_FILENAME
    link_cache = load_link_cache(link_cache_path)
    link_stats = {"resolved": 0, "failed": 0, "errors": 0, "cache_hit": 0}

    session = requests.Session()
    session.headers.update({"User-Agent": "newsdata-fetch/1.0 (+stage1 forestry, rss)"})

    per_feed_cap = cfg.get("rss_max_entries_per_feed", 40)
    delay = cfg.get("rss_feed_delay_seconds", 2)
    filter_counts: dict = {}
    new_total = stale_total = in_newsdata_total = already_total = 0

    for i, feed_url in enumerate(feeds, 1):
        feed_title, entries = fetch_feed(session, feed_url, cfg)
        new_f = stale_f = in_nd_f = already_f = filtered_f = 0
        for entry in entries[:per_feed_cap]:
            dt = entry_datetime(entry)
            if dt is not None and dt < cutoff:
                stale_f += 1
                continue

            row = build_row(entry, feed_title, feed_url, today)
            if not row["title"] or not row["link"]:
                continue

            reason = filter_reason(row, eff_cfg)
            if reason:
                filtered_f += 1
                filter_counts[reason] = filter_counts.get(reason, 0) + 1
                continue

            ntitle = normalize_title(row["title"])
            if cross_dedupe and (ntitle in nd_titles or row["link"] in nd_links):
                in_nd_f += 1
                continue

            aid = row["article_id"] or row["link"]
            if by_id.get(aid) is not None or (ntitle and by_title.get(ntitle) is not None):
                already_f += 1
                continue

            resolved = resolve_link(row["link"], cfg, session, link_cache, link_stats)
            if resolved != row["link"]:
                row["link"] = resolved
                real_name = domain_to_source_name(resolved)
                if real_name:
                    row["source_name"] = real_name
            row["description"] = truncate_text(row["description"], cfg.get("summary_max_chars", 300))

            row["keyword"] = ["(rss)"]  # list form for the shared writer
            rows_in_order.append(row)
            by_id[aid] = row
            if ntitle:
                by_title.setdefault(ntitle, row)
            new_f += 1

        label = feed_title or feed_url
        print(f"[{i}/{len(feeds)}] {label[:70]}")
        print(f"    {len(entries)} entries, {new_f} new, {already_f} already in RSS file, "
              f"{in_nd_f} already in NewsData, {stale_f} outside window, {filtered_f} filtered")
        new_total += new_f
        already_total += already_f
        in_newsdata_total += in_nd_f
        stale_total += stale_f
        if i < len(feeds):
            time.sleep(delay)

    save_link_cache(link_cache_path, link_cache)
    if any(link_stats.values()):
        print(f"Aggregator link resolution: {link_stats['resolved']} resolved, "
              f"{link_stats['cache_hit']} from cache, {link_stats['failed']} failed, "
              f"{link_stats['errors']} errors")
    if filter_counts:
        breakdown = ", ".join(f"{r}: {n}" for r, n in sorted(filter_counts.items()))
        print(f"Content filter: {sum(filter_counts.values())} rejected ({breakdown})")

    if new_total:
        tmp = rss_csv.with_suffix(rss_csv.suffix + ".tmp")
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=COLUMNS)
            w.writeheader()
            for row in rows_in_order:
                out = dict(row)
                kw = row["keyword"]
                out["keyword"] = ", ".join(kw) if isinstance(kw, list) else kw
                w.writerow(out)
        os.replace(tmp, rss_csv)
        print(f"{rss_csv.name}: {new_total} new -> {len(rows_in_order)} total rows "
              f"({already_total} already in RSS, {in_newsdata_total} already in NewsData, "
              f"{stale_total} outside window)")
    else:
        print(f"No new RSS rows ({already_total} already in RSS, {in_newsdata_total} already in NewsData, "
              f"{stale_total} outside window).")

    prune(output_dir, cfg.get("prune_weeks", 12), today)
    print("Done.")


if __name__ == "__main__":
    main()
