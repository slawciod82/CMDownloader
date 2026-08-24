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
    resolution: str | None


@dataclass(frozen=True)
class Result:
    downloaded: int = 0
    failed: int = 0


@dataclass(frozen=True)
class AccountResult:
    found: int = 0
    downloaded: int = 0
    failed: int = 0


def line(message=""):
    with PRINT_LOCK:
        print(message, flush=True)


def sanitize_filename(name):
    for char in ["/", "\\", "*", "\t", ":"]:
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


def _normalize_backoff(values, attempts):
    backoff = [int(value) for value in values]
    if not backoff:
        backoff = [0]
    if len(backoff) < attempts:
        backoff.extend([backoff[-1]] * (attempts - len(backoff)))
    return backoff[:attempts]


def config():
    apikeys = list(app_cfg.apikeys)
    names = list(app_cfg.account_names)
    if len(apikeys) != len(names):
        raise ValueError("apikeys and account_names must have the same length")
    if not apikeys:
        raise ValueError("At least one ClickMeeting account is required")

    if "CM_RUN_INTERVAL_SECONDS" in os.environ:
        interval = int(os.environ["CM_RUN_INTERVAL_SECONDS"])
    elif hasattr(app_cfg, "run_interval_seconds"):
        interval = int(app_cfg.run_interval_seconds)
    else:
        interval = int(getattr(app_cfg, "scheduler_interval", 5)) * 60

    default_timeout = int(getattr(app_cfg, "timeout", 15))
    connect_timeout = int(getattr(app_cfg, "connect_timeout", default_timeout))
    read_timeout = int(
        getattr(app_cfg, "read_timeout", max(default_timeout * 10, 300))
    )
    download_retry = int(getattr(app_cfg, "download_retry", 4))
    download_backoff = _normalize_backoff(
        getattr(app_cfg, "download_backoff", [0, 3, 7, 15]),
        download_retry,
    )
    download_limit = int(getattr(app_cfg, "download_limit", 0))

    account_workers = int(
        os.getenv("CM_ACCOUNT_WORKERS", getattr(app_cfg, "account_workers", 1))
    )
    recording_workers_default = int(
        getattr(
            app_cfg,
            "recording_workers_default",
            getattr(
                app_cfg,
                "max_download_workers",
                getattr(app_cfg, "recording_workers", 2),
            ),
        )
    )
    recording_workers_per_account = {
        str(name): int(value)
        for name, value in dict(
            getattr(app_cfg, "recording_workers_per_account", {})
        ).items()
    }
    download_worker_cap = (
        int(os.environ["CM_MAX_DOWNLOAD_WORKERS"])
        if "CM_MAX_DOWNLOAD_WORKERS" in os.environ
        else None
    )

    if interval < 1:
        raise ValueError("scheduler interval must be >= 1 second")
    if connect_timeout < 1 or read_timeout < 1:
        raise ValueError("connect_timeout and read_timeout must be >= 1 second")
    if not 1 <= download_retry <= 16:
        raise ValueError("download_retry must be between 1 and 16")
    if download_limit < 0:
        raise ValueError("download_limit must be >= 0")
    if not 1 <= account_workers <= 16:
        raise ValueError("account_workers must be between 1 and 16")
    if not 1 <= recording_workers_default <= 32:
        raise ValueError("recording_workers_default must be between 1 and 32")
    if download_worker_cap is not None and not 1 <= download_worker_cap <= 32:
        raise ValueError("CM_MAX_DOWNLOAD_WORKERS must be between 1 and 32")
    for account_name, workers in recording_workers_per_account.items():
        if not 1 <= workers <= 32:
            raise ValueError(
                f"recording worker limit for {account_name} must be between 1 and 32"
            )

    if download_worker_cap is not None:
        recording_workers_default = min(
            recording_workers_default,
            download_worker_cap,
        )
        recording_workers_per_account = {
            name: min(workers, download_worker_cap)
            for name, workers in recording_workers_per_account.items()
        }

    return {
        "accounts": list(zip(apikeys, names)),
        "db": os.getenv(
            "CM_STATE_DATABASE_PATH",
            getattr(app_cfg, "state_database_path", "state/cm_downloader.db"),
        ),
        "recordings": os.getenv("CM_RECORDINGS_PATH", app_cfg.path_to_save),
        "interval": interval,
        "request_timeout": (connect_timeout, read_timeout),
        "download_retry": download_retry,
        "download_backoff": download_backoff,
        "download_limit": download_limit,
        "account_workers": min(account_workers, len(apikeys)),
        "recording_workers_default": recording_workers_default,
        "recording_workers_per_account": recording_workers_per_account,
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


def _get_json_list(url, headers, timeout):
    response = requests.get(url, headers=headers, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list):
        raise ValueError(f"Expected list response from {url}")
    return response, data


def _room_name_map(rooms):
    names = {}
    for room in rooms:
        if not isinstance(room, dict) or room.get("id") is None:
            continue
        room_id = str(room["id"])
        names[room_id] = str(room.get("name") or f"room_{room_id}")
    return names


def _with_room_context(raw, conference_id, room_name):
    item = dict(raw)
    item["_conference_id"] = str(conference_id)
    item["_room_name"] = str(room_name)
    return item


def discover_recordings(headers, cfg, account_name):
    timeout = cfg["request_timeout"]
    global_recordings = None

    try:
        response, global_recordings = _get_json_list(
            f"{API_BASE_URL}/conferences/recordings",
            headers,
            timeout,
        )
        line(
            f"[{account_name}] Global recordings endpoint: "
            f"HTTP {response.status_code}, found {len(global_recordings)}"
        )
    except (requests.RequestException, ValueError) as exc:
        line(
            f"{YELLOW}[{account_name}] Global recordings endpoint failed: {exc}. "
            f"Using active-room fallback.{RESET}"
        )

    if global_recordings is not None:
        rooms = None
        room_names = {}
        try:
            _, rooms = _get_json_list(
                f"{API_BASE_URL}/conferences/active",
                headers,
                timeout,
            )
            room_names = _room_name_map(rooms)
        except (requests.RequestException, ValueError) as exc:
            line(
                f"{YELLOW}[{account_name}] Room-name lookup failed: {exc}. "
                f"Using room_<id> fallback names.{RESET}"
            )

        normalized = []
        unresolved = []
        for raw in global_recordings:
            if not isinstance(raw, dict):
                normalized.append(raw)
                continue
            conference_id = raw.get("conference_id") or raw.get("room_id")
            if conference_id is None:
                unresolved.append(raw)
                continue
            room_id = str(conference_id)
            normalized.append(
                _with_room_context(
                    raw,
                    room_id,
                    room_names.get(room_id, f"room_{room_id}"),
                )
            )

        if unresolved:
            if rooms is None:
                _, rooms = _get_json_list(
                    f"{API_BASE_URL}/conferences/active",
                    headers,
                    timeout,
                )
                room_names = _room_name_map(rooms)

            unresolved_by_id = {
                str(raw["id"]): raw
                for raw in unresolved
                if isinstance(raw, dict) and raw.get("id") is not None
            }
            unresolved_without_id = [
                raw
                for raw in unresolved
                if not isinstance(raw, dict) or raw.get("id") is None
            ]

            for room in rooms:
                if not unresolved_by_id:
                    break
                if not isinstance(room, dict) or room.get("id") is None:
                    continue
                room_id = str(room["id"])
                room_name = room_names.get(room_id, f"room_{room_id}")
                _, per_room = _get_json_list(
                    f"{API_BASE_URL}/conferences/{room_id}/recordings",
                    headers,
                    timeout,
                )
                for per_room_raw in per_room:
                    if not isinstance(per_room_raw, dict):
                        continue
                    recording_id = str(per_room_raw.get("id"))
                    source = unresolved_by_id.pop(recording_id, None)
                    if source is None:
                        continue
                    merged = dict(source)
                    merged.update(per_room_raw)
                    normalized.append(
                        _with_room_context(merged, room_id, room_name)
                    )

            normalized.extend(unresolved_by_id.values())
            normalized.extend(unresolved_without_id)

        return normalized, "global"

    line(f"[{account_name}] Using active -> per-room recordings fallback.")
    _, rooms = _get_json_list(
        f"{API_BASE_URL}/conferences/active",
        headers,
        timeout,
    )
    room_names = _room_name_map(rooms)
    normalized = []

    for room in rooms:
        if not isinstance(room, dict) or room.get("id") is None:
            continue
        room_id = str(room["id"])
        room_name = room_names.get(room_id, f"room_{room_id}")
        _, per_room = _get_json_list(
            f"{API_BASE_URL}/conferences/{room_id}/recordings",
            headers,
            timeout,
        )
        for raw in per_room:
            if isinstance(raw, dict):
                normalized.append(_with_room_context(raw, room_id, room_name))
            else:
                normalized.append(raw)

    return normalized, "active/per-room"


def parse_recording(raw, account_name, headers, account_id, store):
    remote_id = str(raw["id"])
    conference_value = (
        raw.get("_conference_id")
        or raw.get("conference_id")
        or raw.get("room_id")
    )
    if conference_value is None:
        raise ValueError(f"recording {remote_id} has no conference_id/room_id")
    conference_id = str(conference_value)

    name = str(
        raw.get("_room_name")
        or raw.get("room_name")
        or raw.get("recording_name")
        or f"room_{conference_id}"
    )
    size = int(raw["recording_file_size"])
    started_value = raw.get("recorder_started") or raw.get("recorder_start_date")
    if not started_value:
        raise ValueError(f"recording {remote_id} has no recorder start timestamp")
    started = datetime.fromisoformat(str(started_value))
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
        recording_url=str(raw.get("recording_url") or ""),
        recording_name=name,
        local_started=started.astimezone(LOCAL_TIMEZONE),
        expected_size=size,
        current_status=row["status"],
        resolution=row["resolution"],
    )


def target_path(recording, root):
    date = recording.local_started.strftime("%Y-%m-%d")
    year = recording.local_started.strftime("%Y")
    month = recording.local_started.strftime("%m")
    time_part = recording.local_started.strftime("%H_%M_%S")
    filename = f"{sanitize_filename(recording.recording_name)} {date} {time_part}.mp4"
    return Path(root) / year / month / date / filename


def refresh_recording_url(recording, timeout):
    response = requests.get(
        f"{API_BASE_URL}/conferences/{recording.conference_id}/recordings",
        headers=recording.headers,
        timeout=timeout,
    )
    response.raise_for_status()
    raw_recordings = response.json()
    if not isinstance(raw_recordings, list):
        raise ValueError("recordings response is not a list")

    for raw in raw_recordings:
        if str(raw.get("id")) == recording.recording_id:
            url = raw.get("recording_url")
            if url:
                return str(url)
            raise ValueError("recording_url is missing")
    raise ValueError(
        f"recording {recording.recording_id} is not present in conference "
        f"{recording.conference_id}"
    )


def delete_remote(recording, timeout):
    try:
        response = requests.delete(
            f"{API_BASE_URL}/conferences/{recording.conference_id}/recordings/{recording.recording_id}",
            headers=recording.headers,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        return False, f"ClickMeeting DELETE request failed: {exc}"

    if response.ok:
        return True, f"ClickMeeting DELETE returned HTTP {response.status_code}"
    if response.status_code == 404:
        return True, "Remote recording is already absent (HTTP 404)"
    return False, f"ClickMeeting DELETE returned HTTP {response.status_code}"


def discard_short_recording(store, recording, cfg):
    line(
        f"Discarding short recording without download: "
        f"{recording.account_name} / {recording.recording_name} "
        f"({format_size(recording.expected_size)} <= "
        f"{format_size(cfg['download_limit'])})"
    )
    ok, message = delete_remote(recording, cfg["request_timeout"])
    if ok:
        store.mark_discarded(recording.local_id)
        line(f"{GREEN}Short recording discarded{RESET}")
        return 0
    store.mark_discard_delete_failed(recording.local_id, message)
    line(f"{RED}--- ERROR: {message} ---{RESET}")
    return 1


def retry_delete(store, recording, cfg):
    short_discard = recording.resolution == "discarded_short_recording"
    action = (
        "Retrying short-recording discard"
        if short_discard
        else "Retrying remote delete without redownload"
    )
    line(f"{action}: {recording.account_name} / {recording.recording_name}")
    ok, message = delete_remote(recording, cfg["request_timeout"])
    if ok:
        if short_discard:
            store.mark_discarded(
                recording.local_id,
                message=(
                    "Remote deletion retry completed for a recording at or below "
                    "download_limit. No local copy was expected."
                ),
            )
        else:
            store.delete_retry_ok(recording.local_id)
        line(f"{GREEN}Remote delete completed{RESET}")
        return 0
    store.delete_retry_failed(recording.local_id, message)
    line(f"{RED}--- ERROR: {message} ---{RESET}")
    return 1


def _download_attempt(store, worker, recording, part, cfg, single_worker_cli):
    downloaded = 0
    speed = 0.0
    speed_time = state_time = log_time = time.monotonic()
    speed_bytes = 0

    url = refresh_recording_url(recording, cfg["request_timeout"])
    if part.exists():
        part.unlink()

    if single_worker_cli:
        progress(0, recording.expected_size, 0)

    with requests.get(
        url,
        stream=True,
        timeout=cfg["request_timeout"],
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
    return downloaded


def download_one(store, run_id, recording, cfg, single_worker_cli):
    worker = threading.current_thread().name
    path = target_path(recording, cfg["recordings"])
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

            ok, message = delete_remote(recording, cfg["request_timeout"])
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

        store.begin_download(recording.local_id)
        store.worker_start(worker, run_id, recording.local_id, recording.expected_size)

        last_error = None
        for attempt in range(cfg["download_retry"]):
            if attempt > 0:
                backoff = cfg["download_backoff"][attempt]
                if backoff > 0:
                    line(
                        f"[{worker}] Retry {attempt}/{cfg['download_retry'] - 1} "
                        f"for {recording.recording_name}; waiting {backoff}s"
                    )
                    time.sleep(backoff)

            try:
                _download_attempt(
                    store,
                    worker,
                    recording,
                    part,
                    cfg,
                    single_worker_cli,
                )
                last_error = None
                break
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                line(
                    f"{YELLOW}[{worker}] Download attempt {attempt + 1}/"
                    f"{cfg['download_retry']} failed: {exc}{RESET}"
                )
        else:
            message = (
                f"Download failed after {cfg['download_retry']} attempts: {last_error}. "
                "Will retry on a later cycle."
            )
            store.mark_retryable(recording.local_id, message)
            line(f"{RED}--- ERROR: {message} ---{RESET}")
            return Result(failed=1)

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
        ok, message = delete_remote(recording, cfg["request_timeout"])
        if ok:
            store.mark_completed(recording.local_id, str(path))
            line(f"{GREEN}Record Deleted{RESET}")
            return Result(downloaded=1)

        store.mark_delete_failed(recording.local_id, str(path), message)
        line(f"{RED}--- ERROR: {message} ---{RESET}")
        return Result(downloaded=1, failed=1)

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


def recording_worker_limit(cfg, account_name):
    return cfg["recording_workers_per_account"].get(
        account_name,
        cfg["recording_workers_default"],
    )


def process_account(store, run_id, cfg, number, apikey, account_name):
    headers = {"X-Api-Key": apikey}
    account_id = store.ensure_account(account_name)
    line()
    line("=" * 70)
    line(f"Account {number}: {account_name}")
    line("Connecting to ClickMeeting API...")

    try:
        raw_recordings, discovery_mode = discover_recordings(
            headers,
            cfg,
            account_name,
        )
    except (requests.RequestException, ValueError) as exc:
        line(f"{RED}--- ERROR: API request failed for {account_name}: {exc} ---{RESET}")
        return AccountResult(failed=1)

    found = len(raw_recordings)
    failed = downloaded = 0
    line(f"[{account_name}] Recordings found: {found} ({discovery_mode})")
    remote_ids = {
        str(item["id"])
        for item in raw_recordings
        if isinstance(item, dict) and item.get("id") is not None
    }
    store.reconcile_account(account_id, remote_ids)

    queue = []
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
            failed += retry_delete(store, recording, cfg)
        elif recording.current_status in {
            "COMPLETED",
            "COMPLETED_MANUAL_DELETE",
            "RESOLVED_EXTERNALLY",
        }:
            line(
                f"Skipping resolved: {recording.recording_name} "
                f"({recording.current_status})"
            )
        elif (
            cfg["download_limit"] > 0
            and recording.expected_size <= cfg["download_limit"]
        ):
            failed += discard_short_recording(store, recording, cfg)
        elif recording.current_status in {"NEW", "RETRYABLE_ERROR", "DOWNLOADING"}:
            queue.append(recording)
        else:
            line(
                f"Skipping unsupported state {recording.current_status}: "
                f"{recording.recording_name}"
            )

    if not queue:
        line(f"[{account_name}] No recordings queued for download.")
        return AccountResult(found=found, failed=failed)

    workers = min(recording_worker_limit(cfg, account_name), len(queue))
    line(
        f"[{account_name}] Download queue: {len(queue)}, "
        f"recording worker limit: {workers}"
    )
    single_worker_cli = (
        cfg["account_workers"] == 1
        and workers == 1
        and sys.stdout.isatty()
    )
    prefix = f"recording-{sanitize_filename(account_name)}"

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=prefix) as pool:
        futures = [
            pool.submit(
                download_one,
                store,
                run_id,
                recording,
                cfg,
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

    return AccountResult(found=found, downloaded=downloaded, failed=failed)


def run_cycle(store, run_id, cfg):
    found = downloaded = failed = 0
    account_workers = min(cfg["account_workers"], len(cfg["accounts"]))
    line(f"Account worker limit: {account_workers}")

    with ThreadPoolExecutor(
        max_workers=account_workers,
        thread_name_prefix="account",
    ) as pool:
        futures = {
            pool.submit(
                process_account,
                store,
                run_id,
                cfg,
                number,
                apikey,
                account_name,
            ): account_name
            for number, (apikey, account_name) in enumerate(cfg["accounts"], start=1)
        }

        for future in as_completed(futures):
            account_name = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                line(
                    f"{RED}--- ERROR: account worker crashed for "
                    f"{account_name}: {exc} ---{RESET}"
                )
                failed += 1
                continue
            found += result.found
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
            line(f"Account workers: {cfg['account_workers']}")
            line(
                f"Recording workers default: {cfg['recording_workers_default']}"
            )
            if cfg["recording_workers_per_account"]:
                line(
                    "Recording workers per account: "
                    + ", ".join(
                        f"{name}={workers}"
                        for name, workers in sorted(
                            cfg["recording_workers_per_account"].items()
                        )
                    )
                )
            line(
                "Request timeout: "
                f"connect={cfg['request_timeout'][0]}s, "
                f"read={cfg['request_timeout'][1]}s"
            )
            line(
                f"Download retry: {cfg['download_retry']} attempts, "
                f"backoff={cfg['download_backoff']}"
            )
            line(f"Run interval: {cfg['interval']}s")
            if cfg["download_limit"] > 0:
                line(
                    "Short-recording discard threshold: "
                    f"{format_size(cfg['download_limit'])} "
                    f"({cfg['download_limit']} B). Recordings at or below this size "
                    "are intentionally deleted without downloading."
                )

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
