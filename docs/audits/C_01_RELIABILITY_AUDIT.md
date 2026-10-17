# Gate C-01 — CMDownloader reliability audit

Status: **AUDIT COMPLETE — FIXES NOT IMPLEMENTED**.
Source reviewed: `feat/independent-discovery-gate-a` (Gate A deployment; branch must be reconciled with `main` before release).

Scope: static review of `run.py`, `state_store.py`, `models.py`, `docker-compose.yml` and `Dockerfile`. No integration/load/fault-injection tests run. Operator owns production validation.

## Findings

### P0 — DELETE retry does not revalidate the local media file
- Evidence: `run.py:542-568` `retry_delete` calls `delete_remote` directly for non-discard recordings; there is no fresh `stat`/file-integrity verification. `state_store.py:279-304` records the local pathname for DELETE_FAILED but the retry path does not use it.
- Impact: if the file was removed, truncated or replaced between first download and later DELETE retry, the remote recording can be irreversibly deleted despite a missing/invalid local copy.
- Fix: for `DELETE`, recheck trusted `Recording.local_path` with size equality to expected value, constrain to approved /UPLOAD root, and quarantine without DELETE when the file is missing/invalid. Explicitly exempt `DISCARD` and `discarded_short_recording` (policy deliberately deletes <16MB with no copy). Consider stronger integrity verification depending on business requirements.
- Gate: demonstrate withheld DELETE for missing/truncated file and approved retry for valid local copy using mocks / isolated test data.

### P1 — target path may collide for distinct remote recordings
- Evidence: `run.py:476-483` `target_path` uses sanitized room name and timestamp, without recording ID. `run.py:631-762` treats same-size existing file as reusable and may subsequently DELETE remote.
- Impact: distinct recordings with identical name + timestamp + size may be incorrectly treated as the same recording; simultaneous writes to same `.part` may conflict.
- Fix: retain historical naming for existing files and CMSnapshootUI identity; implement canonical file-path reservation/collision detection keyed by account + remote recording ID before writing or issuing DELETE. Resolve new collisions with operator-approved naming strategy.
- Gate: two IDs same name/time/size cannot overwrite, reuse or trigger DELETE for each other.

### P1 — power failure before durable write assurance
- Evidence: `run.py:569-629` writes to `.part` and closes it; `run.py:631-762` compares size, calls `os.replace`, then sends DELETE. No explicit `fsync` / durable-storage guarantee or content checksum is observed.
- Impact: file-size equality alone cannot prove content integrity and a sudden power/storage failure may create ambiguous durability even after a successful rename.
- Fix: document integrity contract; evaluate fsync(file + directory) and checksum where feasible on NFS; never assert full content integrity from size alone.
- Gate: simulate incomplete/corrupt file and storage failures in isolation, with remote DELETE suppressed.

### P1 — restart requeues jobs but discards partial bytes
- Evidence: `state_store.py:29-51` requeues RUNNING jobs; `run.py:569-581` removes preexisting `.part`, and `run.py:631-762` cleans part in finally.
- Impact: correct recovery of job intent but long large transfers restart from 0; no HTTP Range resume. This is primarily a performance/recovery-time limitation.
- Fix: first verify restart idempotency; treat resume support as separate improvement.

### P2 — queue failure reason may be blank
- Evidence: `run.py:830-866` calls `finish_job(job_id, failed, error)` but `error` is only filled in outer `except`. Normal failed `Result` leaves `error=None`. `state_store.py:504-524` writes `last_error=error`.
- Impact: operator panel shows retrying/failed job without its cause even if recording error exists.
- Fix: propagate structured error from worker outcome or derive from persisted Recording.last_error; preserve meaningful last_error for RETRY_WAIT/FAILED.

### P2 — scheduler supervision and observability
- Evidence: `run.py:896-937` scheduler and manager are separate; `download_manager_loop` exceptions in the manager thread can terminate that thread without terminating scheduler. Docker health is based on the parent process only; automatic recovery of a dead manager thread is not assured.
- Impact: discovery can continue while queued tasks stop being processed. UI heartbeat alone is insufficient to guarantee download manager is alive.
- Fix: add manager liveness state, bounded error handling or fail-fast supervision and operator status; test controlled manager failure.

### P2 — configuration drift
- Evidence: `run.py:141-243` accepts `CM_RUN_INTERVAL_SECONDS` but `docker-compose.yml` does not forward that env variable (uses app_cfg default instead).
- Fix: explicitly wire variable or remove unused override; include resolved effective config in startup logs.

## Confirmed controls / retained invariants
- `flock` on the database lockfile prevents two cooperating CMDownloader processes on one state directory.
- Shared lock coordinates thread-local SQLite state store operations; `download_jobs.recording_id` unique helps prevent duplicates.
- Global concurrency cap enforced by ThreadPoolExecutor maximum worker count.
- Discovery is independent from transfer completion; production operator observed multiple 300-second runs while long recordings downloaded.
- `/UPLOAD` is required to be NFS at container startup, and Docker drops capabilities.
- Short recording discard (<= configured threshold) is intentional policy; do not accidentally enforce local-copy requirement on DISCARD.
- CMDownloaderUI remains read-only; CMSnapshootUI unchanged.

## Proposed implementation order
1. C-01a: add safe DELETE retry preconditions (P0); isolated regression cases.
2. C-01b: protect against local target collisions (P1) preserving existing filename convention.
3. C-01c: propagate job error details and add manager liveness supervision (P2).
4. C-02: operator verifies 3+ queued jobs with <=2 simultaneous transfers.
5. C-03: operator verifies restart and job recovery in isolated environment.
6. C-04: operator validates deletion safety with mocked API, never delete real recordings as fault-injection tests.
7. C-05: media/NFS integrity checks, rollback/runbook, operator signoff.

No production deployment or schema migration is part of this audit.
