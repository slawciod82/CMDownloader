from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import fcntl
import os
from pathlib import Path
import sys
import threading
import time
from zoneinfo import ZoneInfo

import requests

import app_cfg
from state_store import StateStore, utc_now_naive


API_BASE_URL = "https://api.clickmeeting.com/v1"
LOCAL_TIMEZONE = ZoneInfo("Europe/Warsaw")
API_TIMEOUT = 30
DOWNLOAD_TIMEOUT = (30, 300)
CHUNK_SIZE = 1024 * 1024
CLI_REFRESH = 0.25
STATE_REFRESH = 1.0
MULTI_LOG_REFRESH = 5.0
HEARTBEAT_SECONDS = 15

USE_COLORS = sys.stdout.isatty() and os.getenv("NO_COLOR") is None
RESET = "\033[0m" if USE_COLORS else ""
GREEN = "\033[92m" if USE_COLORS else ""
CYAN = "\033[96m" if USE_COLORS else ""
YELLOW = "\033[93m" if USE_COLORS else ""
RED = "\033[91m" if USE_COLORS else ""
PRINT_LOCK = threading.Lock()


@dataclass(frozen=True)
class RemoteRecording:
    local_id: int
    account_name: str
    headers: dict[str, str]
    recording_id: str
    conference_id: str
    recording_url: str
    recording_name: str
    local_started: datetime
    expected_size: int
    current_status: str


@dataclass(frozen=True)
class Result:
    downloaded: int = 0
    failed: int = 0


def line(message=""):
    with PRINT_LOCK:
        print(message, flush=True)


def sanitize_filename(name):
    for char in ["/", "\\", "*", "\t"]:
        name = name.replace(char, "-")
    return name


def format_size(value):
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return "--"


def format_speed(value):
    return "--" if value <= 0 else f"{format_size(value)}/s"


def format_eta(seconds):
    if seconds is None or seconds < 0:
        return "--:--"
    hours, remainder = divmod(int(seconds), 3600)
    minutes, seconds = divmod(remainder, 60)
    return (
        f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        if hours
        else f"{minutes:02d}:{seconds:02d}"
    )


def progress(downloaded, total, speed, final=False):
    ratio = min(downloaded / total, 1) if total else 0
    percent = ratio * 100
    width = 30
    filled = int(ratio * width)
    bar = f"{GREEN}{'█' * filled}{RESET}{'░' * (width - filled)}"
    eta = (total - downloaded) / speed if total and speed > 0 else None
    text = (
        f"\r[{bar}] {percent:6.2f}%  "
        f"{format_size(downloaded)} / {format_size(total)}  "
        f"{CYAN}{format_speed(speed):>12}{RESET}  "
        f"ETA {YELLOW}{format_eta(eta)}{RESET}"
    )
    with PRINT_LOCK:
        sys.stdout.write(text + ("\n" if final else ""))
        sys.stdout.flush()


def parse_args():
    parser = argparse.ArgumentParser(description="ClickMeeting recording downloader")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit.")
    parser.add_argument(
        "--init-db",
        action="store_true",
        help="Create the SQLite schema and exit without calling ClickMeeting.",
    )
    return parser.parse_args()


def config():
    apikeys = list(app_cfg.apikeys)
    names = list(app_cfg.account_names)
    if len(apikeys) != len(names):
        raise ValueError("apikeys and account_names must have the same length")
    if not apikeys:
        raise ValueError("At least one ClickMeeting account is required")

    interval = int(
        os.getenv(
            "CM_RUN_INTERVAL_SECONDS",
            getattr(app_cfg, "run_interval_seconds", 300),
        )
    )
    workers = int(
        os.getenv(
            "CM_MAX_DOWNLOAD_WORKERS",
            getattr(app_cfg, "max_download_workers", 2),
        )
    )
    if interval < 1:
        raise ValueError("run_interval_seconds must be >= 1")
    if not 1 <= workers <= 16:
        raise ValueError("max_download_workers must be between 1 and 16")

    return {
        "accounts": list(zip(apikeys, names)),
        "db": os.getenv(
            "CM_STATE_DATABASE_PATH",
            getattr(app_cfg, "state_database_path", "state/cm_downloader.db"),
        ),
        "recordings": os.getenv("CM_RECORDINGS_PATH", app_cfg.path_to_save),
        "interval": interval,
        "workers": workers,
    }


@contextmanager
def process_lock(database_path):
    lock_path = Path(database_path).resolve().with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                f"Another CMDownloader process already holds {lock_path}"
            ) from exc
        yield lock_path
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def heartbeat_loop(store, stop_event):
    while not stop_event.wait(HEARTBEAT_SECONDS):
        try:
            store.touch_heartbeat()
        except Exception as exc:
            line(f"{RED}--- ERROR: heartbeat update failed: {exc} ---{RESET}")


def parse_recording(raw, account_name, headers, account_id, store):
    remote_id = str(raw["id"])
    conference_id = str(raw["conference_id"])
    name = str(raw.get("recording_name") or f"recording_{remote_id}")
    size = int(raw["recording_file_size"])
    started = datetime.fromisoformat(str(raw["recorder_start_date"]))
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)

    row = store.upsert_recording(
        account_id,
        remote_id,
        conference_id,
        name,
        started.astimezone(timezone.utc).replace(tzinfo=None),
        size,
    )
    return RemoteRecording(
        local_id=row["id"],
        account_name=account_name,
        headers=headers,
        recording_id=remote_id,
        conference_id=conference_id,
        recording_url=str(raw["recording_url"]),
        recording_name=name,
        local_started=started.astimezone(LOCAL_TIMEZONE),
        expected_size=size,
        current_status=row["status"],
    )


def target_path(recording, root):
    date = recording.local_started.strftime("%Y-%m-%d")
    time_part = recording.local_started.strftime("%H_%M_%S")
    directory = date.replace("-", "_")
    filename = f"{sanitize_filename(recording.recording_name)} {date} {time_part}.mp4"
    return Path(root) / directory / filename


def delete_remote(recording):
    try:
        response = requests.delete(
            f"{API_BASE_URL}/conferences/{recording.conference_id}/recordings/{recording.recording_id}",
            headers=recording.headers,
            timeout=API_TIMEOUT,
        )
    except requests.RequestException as exc:
        return False, f"ClickMeeting DELETE request failed: {exc}"

    if response.ok:
        return True, f"ClickMeeting DELETE returned HTTP {response.status_code}"
    if response.status_code == 404:
        return True, "Remote recording is already absent (HTTP 404)"
    return False, f"ClickMeeting DELETE returned HTTP {response.status_code}"


def retry_delete(store, recording):
    line(
        f"Retrying remote delete without redownload: "
        f"{recording.account_name} / {recording.recording_name}"
    )
    ok, message = delete_remote(recording)
    if ok:
        store.delete_retry_ok(recording.local_id)
        line(f"{GREEN}Remote delete completed{RESET}")
        return 0
    store.delete_retry_failed(recording.local_id, message)
    line(f"{RED}--- ERROR: {message} ---{RESET}")
    return 1


def download_one(store, run_id, recording, root, single_worker_cli):
    worker = threading.current_thread().name
    path = target_path(recording, root)
    part = Path(f"{path}.part")

    try:
        path.parent.mkdir(parents=True, exist_ok=True)

        line()
        line("-" * 70)
        line(f"[{worker}] {recording.account_name} / {recording.recording_name}")
        line(f"[{worker}] ID {recording.recording_id}, {format_size(recording.expected_size)}")
        line(f"[{worker}] File: {path}")

        if path.exists():
            existing_size = path.stat().st_size
            if existing_size != recording.expected_size:
                message = (
                    f"Existing local file size mismatch "
                    f"(expected {recording.expected_size}, got {existing_size}). "
                    "Automatic overwrite disabled; manual action required."
                )
                store.mark_quarantined(recording.local_id, message)
                line(f"{RED}--- ERROR: {message} ---{RESET}")
                return Result(failed=1)

            ok, message = delete_remote(recording)
            if ok:
                store.mark_completed(
                    recording.local_id,
                    str(path),
                    resolution="recovered_existing_file",
                    message=(
                        "Existing verified local file found. Remote recording was removed "
                        "or already absent; no redownload was performed."
                    ),
                )
                line(f"{GREEN}Existing verified file reused; no redownload{RESET}")
                return Result()
            store.mark_delete_failed(recording.local_id, str(path), message)
            line(f"{RED}--- ERROR: {message} ---{RESET}")
            return Result(failed=1)

        if part.exists():
            part.unlink()

        store.begin_download(recording.local_id)
        store.worker_start(worker, run_id, recording.local_id, recording.expected_size)

        downloaded = 0
        speed = 0.0
        speed_time = state_time = log_time = time.monotonic()
        speed_bytes = 0

        if single_worker_cli:
            progress(0, recording.expected_size, 0)

        with requests.get(
            recording.recording_url,
            stream=True,
            timeout=DOWNLOAD_TIMEOUT,
        ) as response:
            response.raise_for_status()
            with open(part, "wb") as handle:
                for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                    if not chunk:
                        continue
                    handle.write(chunk)
                    downloaded += len(chunk)
                    now = time.monotonic()

                    if now - speed_time >= CLI_REFRESH:
                        elapsed = now - speed_time
                        speed = (downloaded - speed_bytes) / elapsed
                        speed_time, speed_bytes = now, downloaded
                        if single_worker_cli:
                            progress(downloaded, recording.expected_size, speed)

                    if now - state_time >= STATE_REFRESH:
                        store.worker_progress(
                            worker,
                            downloaded,
                            recording.expected_size,
                            speed,
                        )
                        state_time = now

                    if not single_worker_cli and now - log_time >= MULTI_LOG_REFRESH:
                        percent = (
                            min(100, downloaded * 100 / recording.expected_size)
                            if recording.expected_size
                            else 0
                        )
                        line(
                            f"[{worker}] {percent:5.1f}% "
                            f"{format_size(downloaded)} / {format_size(recording.expected_size)} "
                            f"{format_speed(speed)} {recording.recording_name}"
                        )
                        log_time = now

        store.worker_progress(worker, downloaded, recording.expected_size, speed)
        if single_worker_cli:
            progress(downloaded, recording.expected_size, speed, final=True)

        actual_size = part.stat().st_size
        if actual_size != recording.expected_size:
            part.unlink(missing_ok=True)
            message = (
                f"Download failed, file size mismatch "
                f"(expected {recording.expected_size}, got {actual_size}). "
                "Automatic redownload disabled; manual action required."
            )
            store.mark_quarantined(recording.local_id, message)
            line(f"{RED}--- ERROR: {message} ---{RESET}")
            return Result(failed=1)

        os.replace(part, path)
        line(f"{GREEN}Download Completed{RESET}")
        ok, message = delete_remote(recording)
        if ok:
            store.mark_completed(recording.local_id, str(path))
            line(f"{GREEN}Record Deleted{RESET}")
            return Result(downloaded=1)

        store.mark_delete_failed(recording.local_id, str(path), message)
        line(f"{RED}--- ERROR: {message} ---{RESET}")
        return Result(downloaded=1, failed=1)

    except requests.RequestException as exc:
        message = f"Download failed: {exc}. Will retry on a later cycle."
        store.mark_retryable(recording.local_id, message)
        line(f"{RED}--- ERROR: {message} ---{RESET}")
        return Result(failed=1)
    except OSError as exc:
        message = (
            f"Local filesystem error: {exc}. "
            "Automatic retry disabled; manual action required."
        )
        store.mark_quarantined(recording.local_id, message)
        line(f"{RED}--- ERROR: {message} ---{RESET}")
        return Result(failed=1)
    except Exception as exc:
        message = f"Recording processing failed: {exc}"
        store.mark_quarantined(recording.local_id, message)
        line(f"{RED}--- ERROR: {message} ---{RESET}")
        return Result(failed=1)
    finally:
        if part.exists():
            try:
                part.unlink()
            except OSError:
                pass
        try:
            store.worker_stop(worker)
        except Exception as exc:
            line(f"{RED}--- ERROR: worker cleanup failed: {exc} ---{RESET}")


def run_cycle(store, run_id, cfg):
    found = failed = 0
    queue = []

    for number, (apikey, account_name) in enumerate(cfg["accounts"], start=1):
        headers = {"X-Api-Key": apikey}
        account_id = store.ensure_account(account_name)
        line()
        line("=" * 70)
        line(f"Account {number}: {account_name}")
        line("Connecting to ClickMeeting API...")

        try:
            response = requests.get(
                f"{API_BASE_URL}/conferences/recordings",
                headers=headers,
                timeout=API_TIMEOUT,
            )
            response.raise_for_status()
            raw_recordings = response.json()
            if not isinstance(raw_recordings, list):
                raise ValueError("recordings response is not a list")
        except (requests.RequestException, ValueError) as exc:
            line(f"{RED}--- ERROR: API request failed for {account_name}: {exc} ---{RESET}")
            failed += 1
            continue

        found += len(raw_recordings)
        line(f"status code={response.status_code} (ok) for account: {account_name}")
        line(f"Recordings found: {len(raw_recordings)}")
        remote_ids = {
            str(item["id"])
            for item in raw_recordings
            if isinstance(item, dict) and item.get("id") is not None
        }
        store.reconcile_account(account_id, remote_ids)

        for raw in raw_recordings:
            try:
                recording = parse_recording(raw, account_name, headers, account_id, store)
            except (KeyError, TypeError, ValueError) as exc:
                line(f"{RED}--- ERROR: invalid recording metadata: {exc} ---{RESET}")
                failed += 1
                continue

            if recording.current_status == "QUARANTINED":
                line(f"Skipping quarantined: {recording.recording_name}")
            elif recording.current_status == "DELETE_FAILED":
                failed += retry_delete(store, recording)
            elif recording.current_status in {
                "COMPLETED",
                "COMPLETED_MANUAL_DELETE",
                "RESOLVED_EXTERNALLY",
            }:
                line(
                    f"Skipping resolved: {recording.recording_name} "
                    f"({recording.current_status})"
                )
            elif recording.current_status in {"NEW", "RETRYABLE_ERROR", "DOWNLOADING"}:
                queue.append(recording)
            else:
                line(
                    f"Skipping unsupported state {recording.current_status}: "
                    f"{recording.recording_name}"
                )

    if not queue:
        line("No recordings queued for download.")
        return found, 0, failed

    workers = min(cfg["workers"], len(queue))
    line(f"Download queue: {len(queue)}, worker limit: {workers}")
    downloaded = 0
    single_worker_cli = workers == 1 and sys.stdout.isatty()

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="worker") as pool:
        futures = [
            pool.submit(
                download_one,
                store,
                run_id,
                recording,
                cfg["recordings"],
                single_worker_cli,
            )
            for recording in queue
        ]
        for future in as_completed(futures):
            try:
                result = future.result()
            except Exception as exc:
                line(f"{RED}--- ERROR: worker crashed: {exc} ---{RESET}")
                failed += 1
                continue
            downloaded += result.downloaded
            failed += result.failed

    return found, downloaded, failed


def scheduler_loop(store, cfg, once):
    while True:
        run_id = store.start_run()
        line()
        line("=" * 70)
        line(f"Run {run_id} started at {datetime.now(LOCAL_TIMEZONE).isoformat()}")

        try:
            found, downloaded, failed = run_cycle(store, run_id, cfg)
        except Exception as exc:
            next_run = None if once else utc_now_naive() + timedelta(seconds=cfg["interval"])
            store.fail_run(run_id, next_run)
            line(f"{RED}--- ERROR: run {run_id} failed: {exc} ---{RESET}")
            if once:
                raise
        else:
            next_run = None if once else utc_now_naive() + timedelta(seconds=cfg["interval"])
            status = store.finish_run(run_id, found, downloaded, failed, next_run)
            line(
                f"Run {run_id} finished: {status}. "
                f"Found: {found}, downloaded: {downloaded}, problems: {failed}."
            )

        if once:
            return

        line(f"Waiting {cfg['interval']}s before the next ClickMeeting check.")
        time.sleep(cfg["interval"])


def main():
    args = parse_args()
    try:
        cfg = config()
    except (AttributeError, TypeError, ValueError) as exc:
        line(f"{RED}--- CONFIG ERROR: {exc} ---{RESET}")
        return 2

    store = StateStore(cfg["db"])
    stop_heartbeat = threading.Event()

    try:
        with process_lock(cfg["db"]) as lock_path:
            store.initialize(cfg["interval"])
            line(f"Process lock acquired: {lock_path}")

            if args.init_db:
                line(f"State database initialized: {store.database_path}")
                return 0

            line("CMDownloader started")
            line(f"Configured accounts: {len(cfg['accounts'])}")
            line(f"State database: {store.database_path}")
            line(f"Recordings directory: {Path(cfg['recordings']).resolve()}")
            line(f"Download workers: {cfg['workers']}")
            line(f"Run interval: {cfg['interval']}s")

            heartbeat = threading.Thread(
                target=heartbeat_loop,
                args=(store, stop_heartbeat),
                name="heartbeat",
                daemon=True,
            )
            heartbeat.start()
            try:
                scheduler_loop(store, cfg, args.once)
            finally:
                stop_heartbeat.set()
                heartbeat.join(timeout=HEARTBEAT_SECONDS + 1)

    except RuntimeError as exc:
        line(f"{RED}--- ERROR: {exc} ---{RESET}")
        return 3
    except KeyboardInterrupt:
        line("CMDownloader stopped by operator.")
        return 130
    except Exception as exc:
        line(f"{RED}--- ERROR: {exc} ---{RESET}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
