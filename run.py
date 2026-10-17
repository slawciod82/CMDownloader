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
    local_path: str | None = None


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
        "download_worker_cap": download_worker_cap or recording_workers_default,
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
        local_path=row["local_path"],
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


def verify_delete_retry_copy(recording, recordings_root):
    """Fail closed unless the previously verified local copy still exists."""
    if not recording.local_path:
        return "DELETE blocked: missing recorded local_path"
    root = Path(recordings_root).resolve()
    expected_path = target_path(recording, root)
    saved_path = Path(recording.local_path)
    if saved_path.is_symlink() or expected_path.is_symlink():
        return "DELETE blocked: symbolic link at local recording path"
    if saved_path.resolve() != expected_path.resolve():
        return "DELETE blocked: local_path differs from canonical recording path"
    if not expected_path.resolve().is_relative_to(root):
        return "DELETE blocked: local_path is outside recordings root"
    try:
        if not expected_path.is_file():
            return "DELETE blocked: verified local file is missing"
        size = expected_path.stat().st_size
    except OSError as exc:
        return f"DELETE blocked: cannot inspect local recording: {exc}"
    if size != recording.expected_size:
        return f"DELETE blocked: local size mismatch (expected {recording.expected_size}, got {size})"
    return None


def recording_target_collision(store, recording, path):
    """A filename/time collision cannot establish remote recording identity."""
    utc_start = recording.local_started.astimezone(timezone.utc).replace(tzinfo=None)
    for other_id, other_name, other_path in store.get_colliding_recordings(recording.local_id, utc_start):
        other_filename = (
            f"{sanitize_filename(other_name)} "
            f"{recording.local_started.strftime('%Y-%m-%d')} "
            f"{recording.local_started.strftime('%H_%M_%S')}.mp4"
        )
        if other_filename == path.name or (other_path and Path(other_path) == path):
            return f"Target path collision with recording #{other_id}; manual review required"
    return None


def retry_delete(store, recording, cfg):
    short_discard = recording.resolution == "discarded_short_recording"
    action = (
        "Retrying short-recording discard"
        if short_discard
        else "Retrying remote delete without redownload"
    )
    line(f"{action}: {recording.account_name} / {recording.recording_name}")
    if not short_discard:
        reason = verify_delete_retry_copy(recording, cfg["recordings"])
        if reason:
            store.mark_quarantined(recording.local_id, reason)
            line(f"{RED}--- ERROR: {reason} ---{RESET}")
            return 1
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
        collision = recording_target_collision(store, recording, path)
        if collision:
            store.mark_quarantined(recording.local_id, collision)
            line(f"{RED}--- ERROR: {collision} ---{RESET}")
            return Result(failed=1)

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



def discover_account(store, run_id, cfg, number, apikey, account_name):
    """Discovery only: record remote inventory and enqueue work."""
    headers = {"X-Api-Key": apikey}
    account_id = store.ensure_account(account_name)
    try:
        raw_recordings, mode = discover_recordings(headers, cfg, account_name)
    except (requests.RequestException, ValueError) as exc:
        line(f"[{account_name}] Discovery failed: {exc}")
        return AccountResult(failed=1)
    remote_ids = {
        str(item["id"]) for item in raw_recordings
        if isinstance(item, dict) and item.get("id") is not None
    }
    store.reconcile_account(account_id, remote_ids)
    failed = 0
    for raw in raw_recordings:
        try:
            recording = parse_recording(raw, account_name, headers, account_id, store)
        except (KeyError, ValueError, TypeError) as exc:
            line(f"[{account_name}] Invalid recording: {exc}")
            failed += 1
            continue
        if recording.current_status in {
            "COMPLETED", "COMPLETED_MANUAL_DELETE", "RESOLVED_EXTERNALLY", "QUARANTINED",
        }:
            continue
        if recording.current_status == "DELETE_FAILED":
            operation = "DELETE"
        elif cfg["download_limit"] > 0 and recording.expected_size <= cfg["download_limit"]:
            operation = "DISCARD"
        elif recording.current_status in {"NEW", "RETRYABLE_ERROR", "DOWNLOADING"}:
            operation = "DOWNLOAD"
        else:
            continue
        store.enqueue_job(recording.local_id, run_id, operation)
    line(f"[{account_name}] Discovery {mode}: {len(raw_recordings)} recordings")
    return AccountResult(found=len(raw_recordings), failed=failed)


def discovery_cycle(store, run_id, cfg):
    found = failed = 0
    with ThreadPoolExecutor(
        max_workers=cfg["account_workers"], thread_name_prefix="discovery"
    ) as pool:
        futures = [
            pool.submit(discover_account, store, run_id, cfg, n, key, name)
            for n, (key, name) in enumerate(cfg["accounts"], start=1)
        ]
        for future in as_completed(futures):
            try:
                result = future.result()
                found += result.found
                failed += result.failed
            except Exception as exc:
                line(f"Discovery worker failed: {exc}")
                failed += 1
    return found, failed


def _run_job(store, item, cfg, account_keys):
    key = account_keys.get(item["account_name"])
    if not key:
        store.finish_job(item["job_id"], True, "Missing account credentials")
        return
    started = item["recorded_at"].replace(tzinfo=timezone.utc).astimezone(LOCAL_TIMEZONE)
    recording = RemoteRecording(
        local_id=item["recording_pk"],
        account_name=item["account_name"],
        headers={"X-Api-Key": key},
        recording_id=item["recording_id"],
        conference_id=item["conference_id"],
        recording_url="",
        recording_name=item["recording_name"],
        local_started=started,
        expected_size=item["expected_size"],
        current_status=item["status"],
        resolution=item["resolution"],
        local_path=item["local_path"],
    )
    failed = False
    error = None
    try:
        if item["operation"] == "DELETE":
            failed = bool(retry_delete(store, recording, cfg))
        elif item["operation"] == "DISCARD":
            failed = bool(discard_short_recording(store, recording, cfg))
        else:
            result = download_one(store, item["run_id"], recording, cfg, False)
            failed = bool(result.failed)
    except Exception as exc:
        failed = True
        error = str(exc)
        line(f"Job {item['job_id']} failed: {error}")
    finally:
        if failed and not error:
            error = store.get_recording_error(item["recording_pk"]) or "Job failed without recording error"
        store.finish_job(item["job_id"], failed, error)


def download_manager_loop(store, cfg, stop_event):
    account_keys = {name: key for key, name in cfg["accounts"]}
    cap = cfg["download_worker_cap"]
    with ThreadPoolExecutor(max_workers=cap, thread_name_prefix="download") as pool:
        active = {}
        while not stop_event.is_set() or active:
            done = [future for future in active if future.done()]
            for future in done:
                try:
                    future.result()
                except Exception as exc:
                    line(f"Download worker crashed: {exc}")
                del active[future]
            while not stop_event.is_set() and len(active) < cap:
                counts = {}
                for name in active.values():
                    counts[name] = counts.get(name, 0) + 1
                excluded = {
                    name for name, count in counts.items()
                    if count >= recording_worker_limit(cfg, name)
                }
                item = store.claim_job(excluded)
                if item is None:
                    break
                future = pool.submit(_run_job, store, item, cfg, account_keys)
                active[future] = item["account_name"]
            stop_event.wait(0.5)


def scheduler_loop(store, cfg, once):
    """Periodic inventory, never blocked by downloads."""
    stop_downloads = threading.Event()
    manager = None
    if not once:
        manager = threading.Thread(
            target=download_manager_loop, args=(store, cfg, stop_downloads),
            name="download-manager", daemon=False,
        )
        manager.start()
    try:
        while True:
            if manager is not None and not manager.is_alive():
                raise RuntimeError("Download manager stopped unexpectedly; restarting downloader process")
            start = time.monotonic()
            run_id = store.start_run()
            try:
                found, failed = discovery_cycle(store, run_id, cfg)
            except Exception as exc:
                line(f"Discovery run {run_id} failed: {exc}")
                failed = 1
                found = 0
                store.fail_run(
                    run_id, None if once else utc_now_naive() + timedelta(seconds=cfg["interval"])
                )
            else:
                next_run = None if once else utc_now_naive() + timedelta(
                    seconds=max(0, cfg["interval"] - (time.monotonic() - start))
                )
                store.finish_run(run_id, found, 0, failed, next_run)
            if once:
                # Preserve the CLI's one-cycle behavior: finish currently due jobs.
                account_keys = {name: key for key, name in cfg["accounts"]}
                while True:
                    item = store.claim_job()
                    if item is None:
                        return
                    _run_job(store, item, cfg, account_keys)
            remaining = max(0, cfg["interval"] - (time.monotonic() - start))
            # Detect a dead manager promptly even between 300-second discovery cycles.
            until = time.monotonic() + remaining
            while time.monotonic() < until:
                if manager is not None and not manager.is_alive():
                    raise RuntimeError("Download manager stopped unexpectedly; restarting downloader process")
                time.sleep(min(1.0, until - time.monotonic()))
    finally:
        stop_downloads.set()
        if manager is not None:
            manager.join()

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
            line(f"Global download worker cap: {cfg['download_worker_cap']}")
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
