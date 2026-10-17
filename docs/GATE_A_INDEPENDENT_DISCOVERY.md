# Gate A — Independent Discovery and Download

Status: **READY FOR OPERATOR VALIDATION — NOT TESTED**. Do not merge to main before acceptance.

## Scope
- Discovery checks ClickMeeting independently of download completion.
- Durable `download_jobs` table created additively via SQLAlchemy `create_all`; existing tables and recording identity are retained.
- A global download thread pool obeys `CM_MAX_DOWNLOAD_WORKERS`, and existing account-level worker limits.
- Existing `/UPLOAD` target paths, naming, local-file validation and ClickMeeting deletion safeguards are retained.
- CMSnapshootUI: no filesystem path or existing recording identity changes. Its optional metadata enrichment is not yet verified with production data.
- CMDownloaderUI: no changes at Gate A. It may continue to display worker progress; run statistics now describe discovery only.

## Preflight and backup
1. Finish currently active downloads if possible and stop the downloader before migrating.
2. Back up `state/cm_downloader.db` consistently using SQLite backup API (or stop the container and copy the database with any WAL/SHM files).
3. Record the current Docker image/commit and preserve `.env` and `app_cfg.py` separately. Do not publish credentials.
4. Deploy branch `feat/independent-discovery-gate-a` with the same state database and `/UPLOAD` mount.
5. Do not run the old process concurrently. The existing `flock` lock must be held by exactly one instance.

## Operator checks
1. Configure `CM_ACCOUNT_WORKERS=2`, `CM_MAX_DOWNLOAD_WORKERS=2`, `CM_RUN_INTERVAL_SECONDS=300`.
2. Observe a slow large recording; verify Discovery runs every ~300 seconds even while download is active.
3. Verify that identical recordings do not start concurrently or duplicate queue entries.
4. Verify that a free worker starts the next pending task without waiting for Discovery.
5. Stop/restart while downloading; verify that the queue recovers and completed files are not overwritten.
6. Check that remote deletion happens only after local-file size validation and that DELETE_FAILED is not re-downloaded.
7. Verify that a quarantined recording remains skipped and requires operator attention.
8. Confirm the existing `/UPLOAD` path/filename convention still corresponds to the CMSnapshootUI correlation key.
9. Confirm the UI is read-only; any incorrect or stale figures in current UI should be recorded for Gate B.

## Acceptance
Gate A is accepted only after operator verification. No automated tests or server tests have been run by the assistant.

## Rollback
Stop the new container. Restore the previous image/commit **and the backed-up SQLite database as one consistent pair**; do not reuse a post-migration database with the old binary without separate compatibility verification. Preserve newly downloaded media, do not delete or overwrite them during rollback. Reconcile any ClickMeeting deletions made after the backup before running the old downloader.

## Known limits
- Local `.part` files are still discarded by the existing download routine on retry; HTTP Range resume is not implemented.
- A slow transfer occupies its global worker until completion or a request timeout.
- Optional CMSnapshootUI correlation has not been tested with live data.
- Current UI's `runs.downloaded_count` describes no downloads, since downloads now finish outside discovery runs; Gate B will update dashboard semantics.
