#!/usr/bin/env python3
"""
Bring already-collected weekly CSVs up to what a fresh run would produce
today - without querying NewsData.io (no API credits spent):

  - re-applies the current content filter (fetch_news.py's filter_reason()),
    e.g. after adding to blocked_sources or tuning a rule
  - resolves any still-outstanding Google News / NewsBreak / Bundle links to
    their real source (small requests to those sites directly, not NewsData;
    shares the same on-disk cache fetch_news.py uses)
  - truncates description to the current summary_max_chars

Backs up each file it changes to output/backups/ before overwriting (never
clobbers an existing backup - adds a numeric suffix if today's is taken).

Usage:
    python3 retrofilter.py                                        # all weekly CSVs
    python3 retrofilter.py output/newsdata_2026-09-07_to_2026-09-13.csv
"""

import csv
import datetime
import pathlib
import sys

import requests

from fetch_news import (
    COLUMNS,
    LINK_CACHE_FILENAME,
    SCRIPT_DIR,
    domain_to_source_name,
    filter_reason,
    is_bundle_app_link,
    is_google_news_link,
    is_newsbreak_link,
    load_config,
    load_link_cache,
    resolve_link,
    save_link_cache,
    truncate_text,
)


def next_backup_path(backup_dir: pathlib.Path, stem: str, suffix: str) -> pathlib.Path:
    today = datetime.date.today().isoformat()
    candidate = backup_dir / f"{stem}.backup-{today}{suffix}"
    n = 2
    while candidate.exists():
        candidate = backup_dir / f"{stem}.backup-{today}-{n}{suffix}"
        n += 1
    return candidate


def is_aggregator_link(link: str) -> bool:
    return bool(link) and (is_google_news_link(link) or is_newsbreak_link(link) or is_bundle_app_link(link))


def clean_file(path: pathlib.Path, cfg: dict, backup_dir: pathlib.Path,
                session, link_cache: dict, link_stats: dict) -> None:
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    updated = []
    filter_counts: dict = {}
    truncated_count = 0
    changed = False

    for row in rows:
        reason = filter_reason(row, cfg)
        if reason:
            filter_counts[reason] = filter_counts.get(reason, 0) + 1
            changed = True
            continue

        new_row = dict(row)
        link = new_row.get("link", "")
        if is_aggregator_link(link):
            resolved = resolve_link(link, cfg, session, link_cache, link_stats)
            if resolved != link:
                new_row["link"] = resolved
                real_name = domain_to_source_name(resolved)
                if real_name:
                    new_row["source_name"] = real_name

        truncated = truncate_text(new_row.get("description", ""), cfg.get("summary_max_chars", 300))
        if truncated != new_row.get("description", ""):
            new_row["description"] = truncated
            truncated_count += 1

        if new_row != row:
            changed = True
        updated.append(new_row)

    if not changed:
        print(f"{path.name}: {len(rows)} rows, nothing to update")
        return

    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = next_backup_path(backup_dir, path.stem, path.suffix)
    path.rename(backup)

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(updated)

    removed = sum(filter_counts.values())
    breakdown = ", ".join(f"{k}: {v}" for k, v in sorted(filter_counts.items())) if filter_counts else "none"
    print(f"{path.name}: {len(rows)} -> {len(updated)} rows "
          f"(filtered: {breakdown}; {truncated_count} description(s) truncated)")
    print(f"  backup: {backup}")


def main() -> None:
    cfg = load_config(SCRIPT_DIR / "config.toml")
    output_dir = (SCRIPT_DIR / cfg.get("output_dir", "output")).resolve()
    backup_dir = output_dir / "backups"

    args = sys.argv[1:]
    targets = [pathlib.Path(a).resolve() for a in args] if args \
        else sorted(output_dir.glob("newsdata_*_to_*.csv"))

    if not targets:
        sys.exit(f"No weekly CSVs found in {output_dir}")

    session = requests.Session()
    session.headers.update({"User-Agent": "newsdata-fetch/1.0 (+stage1 forestry, retrofilter)"})
    link_cache_path = output_dir / LINK_CACHE_FILENAME
    link_cache = load_link_cache(link_cache_path)
    link_stats = {"resolved": 0, "failed": 0, "errors": 0, "cache_hit": 0}

    for path in targets:
        if not path.exists():
            print(f"{path}: not found, skipping")
            continue
        clean_file(path, cfg, backup_dir, session, link_cache, link_stats)

    save_link_cache(link_cache_path, link_cache)
    if any(link_stats.values()):
        print(f"Aggregator link resolution: {link_stats['resolved']} resolved, "
              f"{link_stats['cache_hit']} from cache, {link_stats['failed']} failed, "
              f"{link_stats['errors']} errors (kept original link)")


if __name__ == "__main__":
    main()
