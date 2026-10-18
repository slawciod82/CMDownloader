# Recording duration — ffprobe

Unit contract: nullable integer `recordings.duration_seconds`, seconds rounded to nearest whole second from MP4 container `format.duration` via ffprobe. UI renders `h:mm:ss`; null renders dash. Never derive duration from download timestamps or file size.

Backend image includes ffmpeg/ffprobe. `StateStore.initialize()` adds the missing column with idempotent SQLite ALTER TABLE; it cannot be rolled back by SQLAlchemy create_all. Existing database backup required before upgrading. After media download or verified existing local file recovery, ffprobe reads metadata and persists duration. Probe failure leaves NULL; it does not trigger redownload or automatic deletion of a otherwise size-verified file. Duration is metadata, not independent integrity verification.

Historical backfill: `scripts/backfill_recording_duration.py` (dry run by default). Operator executes in a controlled window with actual production DB backed up, compatible image with ffprobe and mounted read-only /UPLOAD. For `--apply` coordinate with the downloader as the single DB writer or stop the downloader; never copy/restore stale database over newer live records. Sample usage after schema migration:

```
python scripts/backfill_recording_duration.py --db /state/cm_downloader.db --root /UPLOAD --limit 20
python scripts/backfill_recording_duration.py --db /state/cm_downloader.db --root /UPLOAD --apply
```

Dry-run makes no DB changes. Apply writes only duration_seconds and commits per row. Missing, symlink and outside-root files are skipped. The file remains untouched.

Release ordering: migrate and deploy backend first; verify SQLite has duration_seconds; only then deploy UI using new ORM mapping. UI cannot safely query pre-migration schemas. Backend changes build on the **unmerged** C-01 safety-hardening PR; that PR and Security Baseline Gate require operator approval before deployment. No assistant-side operator tests were performed.

CMSnapshootUI integration is intentionally unchanged. To synchronize fields between products later, use the integer seconds as the canonical unit and a verified stable identity/path join rather than copying file timestamps.
