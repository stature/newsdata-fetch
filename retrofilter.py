#!/usr/bin/env python3
"""
Re-apply the current content filter (fetch_news.py's filter_reason()) to
already-collected weekly CSVs. Useful right after tuning a filter rule, to
clean out rows that were written before the rule existed - fetch_news.py
itself only filters going forward, it never touches past runs.

Backs up each file it changes to output/backups/ before overwriting, so
nothing is ever lost.

Usage:
    python3 retrofilter.py                                        # all weekly CSVs
    python3 retrofilter.py output/newsdata_2026-09-07_to_2026-09-13.csv
"""

import csv
import datetime
import pathlib
import sys

from fetch_news import COLUMNS, SCRIPT_DIR, filter_reason, load_config


def clean_file(path: pathlib.Path, cfg: dict, backup_dir: pathlib.Path) -> None:
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    kept, rejected_counts = [], {}
    for row in rows:
        reason = filter_reason(row, cfg)
        if reason:
            rejected_counts[reason] = rejected_counts.get(reason, 0) + 1
        else:
            kept.append(row)

    if len(kept) == len(rows):
        print(f"{path.name}: {len(rows)} rows, nothing to remove")
        return

    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"{path.stem}.backup-{datetime.date.today().isoformat()}{path.suffix}"
    path.rename(backup)

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(kept)

    removed = sum(rejected_counts.values())
    breakdown = ", ".join(f"{k}: {v}" for k, v in sorted(rejected_counts.items()))
    print(f"{path.name}: {len(rows)} -> {len(kept)} rows ({removed} removed: {breakdown})")
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

    for path in targets:
        if not path.exists():
            print(f"{path}: not found, skipping")
            continue
        clean_file(path, cfg, backup_dir)


if __name__ == "__main__":
    main()
