"""Backfill MP4 durations into CMDownloader SQLite without changing media files.

Run only after operator confirms database backup and filesystem mounts.
Defaults to dry run. Single writer must be stopped for --apply migration.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sqlite3

from run import probe_duration_seconds


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    if not root.is_dir():
        parser.error("recordings root does not exist")
    if args.limit < 0:
        parser.error("--limit must be non-negative")
    # SQLite is read-only during dry runs, including the query itself.
    database = Path(args.db).resolve()
    mode = "rw" if args.apply else "ro"
    db = sqlite3.connect(f"file:{database}?mode={mode}", uri=True, timeout=30)
    try:
        fields = {row[1] for row in db.execute("PRAGMA table_info(recordings)")}
        if "duration_seconds" not in fields:
            parser.error("duration_seconds missing: migrate database before backfill")
        rows = db.execute(
            "SELECT id, local_path FROM recordings "
            "WHERE duration_seconds IS NULL AND local_path IS NOT NULL "
            "ORDER BY id"
        ).fetchall()
        inspected = updated = skipped = 0
        for recording_id, local_path in rows:
            if args.limit and inspected >= args.limit:
                break
            inspected += 1
            path = Path(local_path)
            # Symlinks and outside paths are excluded; never write to media.
            try:
                if path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_file():
                    skipped += 1
                    continue
            except OSError:
                skipped += 1
                continue
            duration = probe_duration_seconds(path)
            if duration is None:
                skipped += 1
                continue
            if args.apply:
                db.execute(
                    "UPDATE recordings SET duration_seconds=? WHERE id=? AND duration_seconds IS NULL",
                    (duration, recording_id),
                )
                db.commit()
            updated += 1
        print(f"mode={'APPLY' if args.apply else 'DRY RUN'} inspected={inspected} "
              f"{'updated' if args.apply else 'would_update'}={updated} skipped={skipped}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
