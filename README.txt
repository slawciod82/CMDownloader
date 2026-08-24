CMDownloader
============

CMDownloader downloads recordings from ClickMeeting accounts, verifies the local file size and only then removes the remote recording.

Runtime model
-------------

The downloader now runs as one long-lived scheduler process:

    check ClickMeeting -> process queue -> wait -> next check

- A Linux flock stored next to the SQLite database prevents a second downloader process from using the same state directory.
- Downloads use a small ThreadPoolExecutor worker pool. The default is 2 parallel workers.
- The next check is scheduled after the previous run finishes, so runs do not overlap.
- A heartbeat is written every 15 seconds.
- CMDownloader is the only writer of the SQLite operational database.
- CMDownloaderUI mounts the same state directory read-only.

Failure behaviour
-----------------

- A network/download request error becomes RETRYABLE_ERROR and can retry on a later cycle.
- A file-size mismatch or local filesystem error becomes QUARANTINED and automatic redownload is disabled.
- If the local file is verified but remote DELETE fails, the state becomes DELETE_FAILED. Later runs retry only DELETE and do not redownload the recording.
- Reconciliation clears operator attention when a quarantined/deletion-failed remote recording disappears from ClickMeeting, while preserving the event history.

Configuration
-------------

1. Copy app_cfg_example.py to app_cfg.py.
2. Enter ClickMeeting API keys.
3. Keep account_names in the same order as apikeys.
4. Set path_to_save.
5. Optionally change:

    state_database_path = "state/cm_downloader.db"
    run_interval_seconds = 300
    max_download_workers = 2

Runtime environment variables can override operational paths/settings:

    CM_STATE_DATABASE_PATH
    CM_RECORDINGS_PATH
    CM_RUN_INTERVAL_SECONDS
    CM_MAX_DOWNLOAD_WORKERS

Requirements
------------

- Linux (the single-process guard uses fcntl/flock)
- Python 3.10 or newer
- packages from requirements.txt

Local setup:

    python -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt

Safe state-schema check without calling ClickMeeting:

    python run.py --init-db

One ClickMeeting cycle, then exit:

    python run.py --once

Normal scheduler mode:

    python run.py

Docker
------

The image exposes no HTTP port. app_cfg.py is mounted read-only; the state and recordings directories are the only writable bind mounts.

Set the recordings directory and optionally a shared state directory:

    export CM_RECORDINGS_DIR=/path/to/recordings
    export CM_STATE_DIR=/var/lib/cm-downloader

Then:

    docker compose build
    docker compose up -d

The container runs as uid 10001. Ensure CM_STATE_DIR and CM_RECORDINGS_DIR are writable by uid 10001 before starting it.

CMDownloaderUI can use the same CM_STATE_DIR variable and mounts that directory read-only.

Important
---------

A successfully downloaded recording is automatically deleted from ClickMeeting after its local size has been verified. Use a test recording for the first real end-to-end run.

app_cfg.py contains API credentials and is ignored by both Git and the Docker build context.
