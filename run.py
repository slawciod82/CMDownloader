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
CLI_PROGRESS_REFRESH_SECONDS = 0.25
STATE_PROGRESS_REFRESH_SECONDS = 1.0
MULTI_PROGRESS_LOG_SECONDS = 5.0
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
    recorded_at_utc: datetime
    local_started: datetime
    expected_size: int
    current_status: str
    local_path: str | None


@dataclass(frozen=True)
class DownloadResult:
    downloaded_count: int = 0
    failed_count: int = 0


def sanitize_filename(name: str) -> str:
    for char in ["/", "\\", "*", "\t"]:
        name = name.replace(char, "-")
    return name


def format_size(size: int | float) -> str:
    size = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return "--"


def format_speed(bytes_per_second: int | float) -> str:
    if bytes_per_second <= 0:
        return "--"
    return f"{format_size(bytes_per_second)}/s"


def format_eta(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "--:--"
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def print_line(message: str = "") -> None:
    with PRINT_LOCK:
        print(message, flush=True)


def print_progress(downloaded: int, total: int, speed: float, final: bool = False) -> None:
    if total > 0:
        ratio = min(downloaded / total, 1)
        percent = ratio * 100
        bar_width = 30
        filled = int(ratio * bar_width)
        bar = f"{GREEN}{'█' * filled}{RESET}{'░' * (bar_width - filled)}"
        eta = (total - downloaded) / speed if speed > 0 else None
        line = (
            f"\r[{bar}] {percent:6.2f}%  "
            f"{format_size(downloaded)} / {format_size(total)}  "
            f"{CYAN}{format_speed(speed):>12}{RESET}  "
            f"ETA {YELLOW}{format_eta(eta)}{RESET}"
        )
    else:
        line = (
            f"\rDownloaded {format_size(downloaded)}  "
            f"{CYAN}{format_speed(speed):>12}{RESET}"
        )

    with PRINT_LOCK:
        sys.stdout.write(line)
        sys.stdout.flush()
        if final:
            sys.stdout.write("\n")
            sys.stdout.flush()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ClickMeeting recording downloader")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one discovery/download cycle and exit.",
    )
    parser.add_argument(
        "--init-db",
        action="store_true",
        help="Create the SQLite state schema and exit without calling ClickMeeting.",
    )
    return parser.parse_args()


def get_config() -> dict:
    apikeys = list(app_cfg.apikeys)
    account_names = list(app_cfg.account_names)

    if len(apikeys) != len(account_names):
        raise ValueError("apikeys and account_names must contain the same number of items")
    if not apikeys:
        raise ValueError("At least one ClickMeeting account must be configured")

    state_database_path = os.getenv(
        "CM_STATE_DATABASE_PATH",
        getattr(app_cfg, "state_database_path", "state/cm_downloader.db"),
    )
    recordings_path = os.getenv("CM_RECORDINGS_PATH", app_cfg.path_to_save)
    run_interval_seconds = int(
        os.getenv(
            "CM_RUN_INTERVAL_SECONDS",
            getattr(app_cfg, "run_interval_seconds", 300),
        )
    )
    max_download_workers = int(
        os.getenv(
            "CM_MAX_DOWNLOAD_WORKERS",
            getattr(app_cfg, "max_download_workers", 2),
        )
    )

    if run_interval_seconds < 1:
        raise ValueError("run_interval_seconds must be at least 1")
    if not 1 <= max_download_workers <= 16:
        raise ValueError("max_download_workers must be between 1 and 16")

    return {
        "accounts": list(zip(apikeys, account_names)),
        "state_database_path": state_database_path,
        "recordings_path": recordings_path,
        "run_interval_seconds": run_interval_seconds,
        "max_download_workers": max_download_workers,
    }


@contextmanager
def process_lock(database_path: str):
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
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def heartbeat_loop(store: StateStore, stop_event: threading.Event) -> None:
    while not stop_event.wait(HEARTBEAT_SECONDS):
        try:
            store.touch_heartbeat()
        except Exception as exc:
            print_line(f"{RED}--- ERROR: heartbeat update failed: {exc} ---{RESET}")


def parse_remote_recording(
    raw: dict,
    account_name: str,
    headers: dict[str, str],
    account_id: int,
    store: StateStore,
) -> RemoteRecording:
    recording_id = str(raw["id"])
    conference_id = str(raw["conference_id"])
    recording_url = str(raw["recording_url"])
    recording_name = str(raw.get("recording_name") or f"recording_{recording_id}")
    expected_size = int(raw["recording_file_size"])

    started = datetime.fromisoformat(str(raw["recorder_start_date"]))
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)

    recorded_at_utc = started.astimezone(timezone.utc).replace(tzinfo=None)
    local_started = started.astimezone(LOCAL_TIMEZONE)

    row = store.upsert_recording(
        account_id=account_id,
        recording_id=recording_id,
        conference_id=conference_id,
        recording_name=recording_name,
        recorded_at=recorded_at_utc,
        expected_size=expected_size,
    )

    return RemoteRecording(
        local_id=row["id"],
        account_name=account_name,
        headers=headers,
        recording_id=recording_id,
        conference_id=conference_id,
        recording_url=recording_url,
        recording_name=recording_name,
        recorded_at_utc=recorded_at_utc,
        local_started=local_started,
        expected_size=expected_size,
        current_status=row["status"],
        local_path=row["local_path"],
    )


def build_target_path(recording: RemoteRecording, recordings_path: str) -> Path:
    rec_date = recording.local_started.strftime("%Y-%m-%d")
    rec_time = recording.local_started.strftime("%H_%M_%S")
    rec_dir = rec_date.replace("-", "_")
    safe_name = sanitize_filename(recording.recording_name)
    filename = f"{safe_name} {rec_date} {rec_time}.mp4"
    return Path(recordings_path) / rec_dir / filename


def delete_remote_recording(recording: RemoteRecording) -> tuple[bool, str]:
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


def retry_remote_delete(store: StateStore, recording: RemoteRecording) -> int:
    print_line(
        f"Retrying remote delete without redownload: "
        f"{recording.account_name} / {recording.recording_name}"
    )
    success, message = delete_remote_recording(recording)
    if success:
        store.delete_retry_ok(recording.local_id)
        print_line(f"{GREEN}Remote delete completed{RESET}")
        return 0

    store.delete_retry_failed(recording.local_id, message)
    print_line(f"{RED}--- ERROR: {message} ---{RESET}")
    return 1


def download_recording(
    store: StateStore,
    run_id: int,
    recording: RemoteRecording,
    recordings_path: str,
    single_worker_cli: bool,
) -> DownloadResult:
    worker_name = threading.current_thread().name
    target_path = build_target_path(recording, recordings_path)
    temp_path = Path(f"{target_path}.part")
    target_path.parent.mkdir(parents=True, exist_ok=True)

    print_line()
    print_line("-" * 70)
    print_line(f"[{worker_name}] Account: {recording.account_name}")
    print_line(f"[{worker_name}] ID: {recording.recording_id}")
    print_line(f"[{worker_name}] Name: {recording.recording_name}")
    print_line(f"[{worker_name}] Recorded: {recording.local_started.isoformat()}")
    print_line(f"[{worker_name}] Original file size: {format_size(recording.expected_size)}")
    print_line(f"[{worker_name}] File: {target_path}")

    try:
        if target_path.exists():
            existing_size = target_path.stat().st_size
            if existing_size != recording.expected_size:
                message = (
                    "Existing local file size mismatch "
                    f"(expected {recording.expected_size}, got {existing_size}). "
                    "Automatic overwrite disabled; manual action required."
                )
                store.mark_quarantined(recording.local_id, message)
                print_line(f"{RED}--- ERROR: {message} ---{RESET}")
                return DownloadResult(failed_count=1)

            print_line(
                f"[{worker_name}] Existing verified local file found; "
                "skipping redownload and checking remote deletion."
            )
            success, delete_message = delete_remote_recording(recording)
            if success:
                store.mark_completed(
                    recording.local_id,
                    str(target_path),
                    resolution="recovered_existing_file",
                    message=(
                        "Existing verified local file found. Remote recording is no longer "
                        "present after delete/reconciliation; no redownload was performed."
                    ),
                )
                print_line(f"{GREEN}Record completed without redownload{RESET}")
                return DownloadResult()

            store.mark_delete_failed(
                recording.local_id,
                str(target_path),
                delete_message,
            )
            print_line(f"{RED}--- ERROR: {delete_message} ---{RESET}")
            return DownloadResult(failed_count=1)

        if temp_path.exists():
            temp_path.unlink()

        store.begin_download(recording.local_id)
        store.worker_start(
            worker_name,
            run_id,
            recording.local_id,
            recording.expected_size,
        )

        downloaded_bytes = 0
        current_speed = 0.0
        last_speed_time = time.monotonic()
        last_speed_bytes = 0
        last_state_time = last_speed_time
        last_multi_log_time = last_speed_time

        if single_worker_cli:
            print_progress(0, recording.expected_size, 0)

        with requests.get(
            recording.recording_url,
            stream=True,
            timeout=DOWNLOAD_TIMEOUT,
        ) as response:
            response.raise_for_status()
            with open(temp_path, "wb") as file_handle:
                for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                    if not chunk:
                        continue

                    file_handle.write(chunk)
                    downloaded_bytes += len(chunk)
                    now = time.monotonic()

                    speed_elapsed = now - last_speed_time
                    if speed_elapsed >= CLI_PROGRESS_REFRESH_SECONDS:
                        current_speed = (
                            downloaded_bytes - last_speed_bytes
                        ) / speed_elapsed
                        last_speed_time = now
                        last_speed_bytes = downloaded_bytes

                        if single_worker_cli:
                            print_progress(
                                downloaded_bytes,
                                recording.expected_size,
                                current_speed,
                            )

                    if now - last_state_time >= STATE_PROGRESS_REFRESH_SECONDS:
                        store.worker_progress(
                            worker_name,
                            downloaded_bytes,
                            recording.expected_size,
                            current_speed,
                        )
                        last_state_time = now

                    if (
                        not single_worker_cli
                        and now - last_multi_log_time >= MULTI_PROGRESS_LOG_SECONDS
                    ):
                        percent = (
                            min(100.0, downloaded_bytes * 100 / recording.expected_size)
                            if recording.expected_size
                            else 0.0
                        )
                        print_line(
                            f"[{worker_name}] {percent:5.1f}% "
                            f"{format_size(downloaded_bytes)} / "
                            f"{format_size(recording.expected_size)} "
                            f"{format_speed(current_speed)} "
                            f"{recording.recording_name}"
                        )
                        last_multi_log_time = now

        store.worker_progress(
            worker_name,
            downloaded_bytes,
            recording.expected_size,
            current_speed,
        )
        if single_worker_cli:
            print_progress(
                downloaded_bytes,
                recording.expected_size,
                current_speed,
                final=True,
            )

        file_size = temp_path.stat().st_size
        if file_size != recording.expected_size:
            temp_path.unlink(missing_ok=True)
            message = (
                "Download failed, file size mismatch "
                f"(expected {recording.expected_size}, got {file_size}). "
                "Automatic redownload disabled; manual action required."
            )
            store.mark_quarantined(recording.local_id, message)
            print_line(f"{RED}--- ERROR: {message} ---{RESET}")
            return DownloadResult(failed_count=1)

        os.replace(temp_path, target_path)
        print_line(f"{GREEN}Download Completed{RESET}")
        print_line("File size verified. Deleting verified recording from ClickMeeting...")

        success, delete_message = delete_remote_recording(recording)
        if success:
            store.mark_completed(recording.local_id, str(target_path))
            print_line(f"{GREEN}Record Deleted{RESET}")
            return DownloadResult(downloaded_count=1)

        store.mark_delete_failed(
            recording.local_id,
            str(target_path),
            delete_message,
        )
        print_line(f"{RED}--- ERROR: {delete_message} ---{RESET}")
        return DownloadResult(downloaded_count=1, failed_count=1)

    except requests.RequestException as exc:
        message = f"Download failed: {exc}. Will retry on a later cycle."
        store.mark_retryable(recording.local_id, message)
        print_line(f"{RED}--- ERROR: {message} ---{RESET}")
        return DownloadResult(failed_count=1)

    except OSError as exc:
        message = (
            f"Local filesystem error: {exc}. "
            "Automatic retry disabled; manual action required."
        )
        store.mark_quarantined(recording.local_id, message)
        print_line(f"{RED}--- ERROR: {message} ---{RESET}")
        return DownloadResult(failed_count=1)

    except Exception as exc:
        message = f"Recording processing failed: {exc}"
        store.mark_quarantined(recording.local_id, message)
        print_line(f"{RED}--- ERROR: {message} ---{RESET}")
        return DownloadResult(failed_count=1)

    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        try:
            store.worker_stop(worker_name)
        except Exception as exc:
            print_line(f"{RED}--- ERROR: worker state cleanup failed: {exc} ---{RESET}")


def run_cycle(store: StateStore, run_id: int, config: dict) -> tuple[int, int, int]:
    recordings_found = 0
    failed_count = 0
    download_tasks: list[RemoteRecording] = []

    for account_number, (apikey, account_name) in enumerate(config["accounts"], start=1):
        headers = {"X-Api-Key": apikey}
        account_id = store.ensure_account(account_name)

        print_line()
        print_line("=" * 70)
        print_line(f"Account {account_number}: {account_name}")
        print_line("Connecting to ClickMeeting API...")

        try:
            response = requests.get(
                f"{API_BASE_URL}/conferences/recordings",
                headers=headers,
                timeout=API_TIMEOUT,
            )
            response.raise_for_status()
            raw_recordings = response.json()
            if not isinstance(raw_recordings, list):
                raise ValueError("ClickMeeting recordings response is not a list")
        except (requests.RequestException, ValueError) as exc:
            print_line(
                f"{RED}--- ERROR: API request failed for account: "
                f"{account_name}: {exc} ---{RESET}"
            )
            failed_count += 1
            continue

        print_line(f"status code={response.status_code} (ok) for account: {account_name}")
        print_line(f"Recordings found: {len(raw_recordings)}")
        recordings_found += len(raw_recordings)

        remote_ids = {
            str(raw["id"])
            for raw in raw_recordings
            if isinstance(raw, dict) and raw.get("id") is not None
        }
        store.reconcile_account(account_id, remote_ids)

        for raw in raw_recordings:
            try:
                recording = parse_remote_recording(
                    raw,
                    account_name,
                    headers,
                    account_id,
                    store,
                )
            except (KeyError, TypeError, ValueError) as exc:
                print_line(
                    f"{RED}--- ERROR: Invalid recording metadata for "
                    f"{account_name}: {exc} ---{RESET}"
                )
                failed_count += 1
                continue

            if recording.current_status == "QUARANTINED":
                print_line(
                    f"Skipping quarantined recording: "
                    f"{account_name} / {recording.recording_name}"
                )
                continue

            if recording.current_status == "DELETE_FAILED":
                failed_count += retry_remote_delete(store, recording)
                continue

            if recording.current_status in {
                "COMPLETED",
                "COMPLETED_MANUAL_DELETE",
                "RESOLVED_EXTERNALLY",
            }:
                print_line(
                    f"Skipping already resolved recording: "
                    f"{account_name} / {recording.recording_name} "
                    f"({recording.current_status})"
                )
                continue

            if recording.current_status in {"NEW", "RETRYABLE_ERROR", "DOWNLOADING"}:
                download_tasks.append(recording)
                continue

            print_line(
                f"Skipping recording with unsupported state "
                f"{recording.current_status}: {recording.recording_name}"
            )

    if not download_tasks:
        print_line()
        print_line("No recordings queued for download.")
        return recordings_found, 0, failed_count

    max_workers = min(config["max_download_workers"], len(download_tasks))
    print_line()
    print_line(
        f"Download queue: {len(download_tasks)} recording(s), "
        f"worker limit: {max_workers}"
    )

    downloaded_count = 0
    single_worker_cli = max_workers == 1 and sys.stdout.isatty()

    with ThreadPoolExecutor(
        max_workers=max_workers,
        thread_name_prefix="worker",
    ) as executor:
        futures = [
            executor.submit(
                download_recording,
                store,
                run_id,
                recording,
                config["recordings_path"],
                single_worker_cli,
            )
            for recording in download_tasks
        ]

        for future in as_completed(futures):
            result = future.result()
            downloaded_count += result.downloaded_count
            failed_count += result.failed_count

    return recordings_found, downloaded_count, failed_count


def scheduler_loop(store: StateStore, config: dict, once: bool) -> None:
    while True:
        run_id = store.start_run()
        print_line()
        print_line("=" * 70)
        print_line(f"Run {run_id} started at {datetime.now(LOCAL_TIMEZONE).isoformat()}")

        try:
            recordings_found, downloaded_count, failed_count = run_cycle(
                store,
                run_id,
                config,
            )
        except Exception as exc:
            next_run_at = (
                None
                if once
                else utc_now_naive() + timedelta(seconds=config["run_interval_seconds"])
            )
            store.fail_run(run_id, next_run_at)
            print_line(f"{RED}--- ERROR: run {run_id} failed: {exc} ---{RESET}")
            if once:
                raise
        else:
            next_run_at = (
                None
                if once
                else utc_now_naive() + timedelta(seconds=config["run_interval_seconds"])
            )
            status = store.finish_run(
                run_id,
                recordings_found,
                downloaded_count,
                failed_count,
                next_run_at,
            )
            print_line()
            print_line(
                f"Run {run_id} finished: {status}. "
                f"Found: {recordings_found}, downloaded: {downloaded_count}, "
                f"problems: {failed_count}."
            )

        if once:
            return

        print_line(
            f"Waiting {config['run_interval_seconds']}s before the next ClickMeeting check."
        )
        time.sleep(config["run_interval_seconds"])


def main() -> int:
    args = parse_args()

    try:
        config = get_config()
    except (AttributeError, TypeError, ValueError) as exc:
        print_line(f"{RED}--- CONFIG ERROR: {exc} ---{RESET}")
        return 2

    store = StateStore(config["state_database_path"])
    store.initialize(config["run_interval_seconds"])

    if args.init_db:
        print_line(f"State database initialized: {store.database_path}")
        return 0

    print_line("CMDownloader started")
    print_line(f"Configured accounts: {len(config['accounts'])}")
    print_line(f"State database: {store.database_path}")
    print_line(f"Recordings directory: {Path(config['recordings_path']).resolve()}")
    print_line(f"Download workers: {config['max_download_workers']}")
    print_line(f"Run interval: {config['run_interval_seconds']}s")

    stop_heartbeat = threading.Event()

    try:
        with process_lock(config["state_database_path"]) as lock_path:
            print_line(f"Process lock acquired: {lock_path}")
            heartbeat = threading.Thread(
                target=heartbeat_loop,
                args=(store, stop_heartbeat),
                name="heartbeat",
                daemon=True,
            )
            heartbeat.start()

            try:
                scheduler_loop(store, config, once=args.once)
            finally:
                stop_heartbeat.set()
                heartbeat.join(timeout=HEARTBEAT_SECONDS + 1)

    except RuntimeError as exc:
        print_line(f"{RED}--- ERROR: {exc} ---{RESET}")
        return 3
    except KeyboardInterrupt:
        print_line()
        print_line("CMDownloader stopped by operator.")
        return 130
    except Exception as exc:
        print_line(f"{RED}--- ERROR: {exc} ---{RESET}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
