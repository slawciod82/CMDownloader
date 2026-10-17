# C-01a–c — safety hardening implementation

Source: `feat/c-01-safety-hardening` (branched from reviewed Gate A + C-01 audit).
**Do not deploy directly to production** until operator-run tests and security baseline gate pass.

## Changes
- `DELETE` retry checks persisted local path, exact expected target path, file presence, size, symlink status and approved recordings root before issuing ClickMeeting DELETE.
- Intentional short-recording `DISCARD` remains exempt; configured <=16 MB policy unchanged.
- If check fails, recording becomes `QUARANTINED`, operator attention requested and job ends `FAILED` rather than retrying/delete.
- Download path collision detection before opening/reusing `.mp4` or `.part`; distinct known recordings matching final pathname are quarantined. Existing canonical naming is unchanged.
- Failed worker outcomes propagate `recordings.last_error` into `download_jobs.last_error` to improve UI visibility.
- Scheduler detects unexpected death of Download Manager and exits; Docker restart policy can recover it.
- Docker Compose now forwards `CM_RUN_INTERVAL_SECONDS` (default 300).
- Added `tests/test_c01_safety.py` with isolated non-network regression cases (not executed by assistant).

## Limits and additional hardening
- The current verification contract compares size and location, not a cryptographic digest of media. Corruption preserving file length is not detected.
- NFS crash durability is not guaranteed by size alone; fsync/digest policy awaits decision.
- Cross-record collision detection relies on recording metadata within a +/-3h timestamp window and path conventions. Complex filename changes or historical paths still require operator reconciliation.
- No HTTP Range resume; existing `.part` is removed on restart.
- Manager liveness restart may be delayed by unusually slow discovery, and does not cover all kinds of stalled-but-alive worker threads.
- Existing local file recovery still accepts same-size files when no competing known recording; checksum-level proof is not provided.

## Operator test order
1. Use isolated SQLite and fake API (never actual ClickMeeting DELETE).
2. Run `python -m unittest discover -s tests -p 'test_c01_safety.py'` in a Python environment with project dependencies.
3. Verify missing/truncated/foreign/symlink local file blocks DELETE; valid file allows *only the precondition*.
4. Verify short recording DISCARD is untouched; review server-side policy.
5. Verify different recording IDs with same name and timestamp cannot overwrite or reuse one another.
6. Verify error message appears in `download_jobs.last_error`.
7. Exercise worker failure and restart with snapshots, not live recordings.
8. Run Security Baseline Gate before using production credentials or enabling any real DELETE.
9. Deploy in planned maintenance window only, taking a fresh consistent SQLite backup after stopping backend, preserving prior image and media.

Do not merge this PR or alter running Gate A until operator signoff.
