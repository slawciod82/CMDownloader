from datetime import datetime
import os
import sys
import time
from zoneinfo import ZoneInfo

import requests

import app_cfg


API_BASE_URL = "https://api.clickmeeting.com/v1"
LOCAL_TIMEZONE = ZoneInfo("Europe/Warsaw")
API_TIMEOUT = 30
DOWNLOAD_TIMEOUT = (30, 300)
CHUNK_SIZE = 1024 * 1024
PROGRESS_REFRESH_SECONDS = 0.25

USE_COLORS = sys.stdout.isatty() and os.getenv("NO_COLOR") is None
RESET = "\033[0m" if USE_COLORS else ""
GREEN = "\033[92m" if USE_COLORS else ""
CYAN = "\033[96m" if USE_COLORS else ""
YELLOW = "\033[93m" if USE_COLORS else ""
RED = "\033[91m" if USE_COLORS else ""


def sanitize_filename(name):
    for char in ["/", "\\", "*", "\t"]:
        name = name.replace(char, "-")
    return name


def format_size(size):
    size = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024


def format_speed(bytes_per_second):
    if bytes_per_second <= 0:
        return "--"
    return f"{format_size(bytes_per_second)}/s"


def format_eta(seconds):
    if seconds is None or seconds < 0:
        return "--:--"
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def print_progress(downloaded, total, speed, final=False):
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

    sys.stdout.write(line)
    sys.stdout.flush()
    if final:
        sys.stdout.write("\n")
        sys.stdout.flush()


apikeys = app_cfg.apikeys
account_names = app_cfg.account_names

print("CMDownloader started")
print(f"Configured accounts: {len(list(zip(apikeys, account_names)))}")

for account_number, (apikey, account_name) in enumerate(
    zip(apikeys, account_names), start=1
):
    headers = {"X-Api-Key": apikey}
    print()
    print("=" * 70)
    print(f"Account {account_number}: {account_name}")
    print("Connecting to ClickMeeting API...")

    try:
        response = requests.get(
            f"{API_BASE_URL}/conferences/recordings",
            headers=headers,
            timeout=API_TIMEOUT,
        )
        response.raise_for_status()
        recordings = response.json()
        print(
            f"status code={response.status_code} (ok) for account: {account_name}"
        )
        print(f"Recordings found: {len(recordings)}")
    except (requests.RequestException, ValueError) as exc:
        print(
            f"{RED}--- ERROR: API request failed for account: "
            f"{account_name}: {exc} ---{RESET}"
        )
        continue

    if not recordings:
        print("Nothing to download for this account.")
        continue

    downloaded_count = 0
    deleted_count = 0

    for recording_number, rec in enumerate(recordings, start=1):
        temp_path = None
        progress_active = False
        print()
        print("-" * 70)
        print(f"Recording {recording_number}/{len(recordings)}")

        try:
            rec_id = rec["id"]
            conference_id = rec["conference_id"]
            rec_url = rec["recording_url"]
            rec_started = datetime.fromisoformat(rec["recorder_start_date"])
            rec_name = sanitize_filename(
                rec.get("recording_name") or f"recording_{rec_id}"
            )
            rec_file_size = int(rec["recording_file_size"])

            local_started = rec_started.astimezone(LOCAL_TIMEZONE)
            rec_date = local_started.strftime("%Y-%m-%d")
            rec_time = local_started.strftime("%H_%M_%S")
            rec_dir = rec_date.replace("-", "_")

            path_to_save_as = app_cfg.path_to_save
            target_dir = os.path.join(path_to_save_as, rec_dir)
            os.makedirs(target_dir, exist_ok=True)

            file_to_save_as = f"{rec_name} {rec_date} {rec_time}.mp4"
            path = os.path.join(target_dir, file_to_save_as)
            temp_path = f"{path}.part"

            print(f"ID: {rec_id}")
            print(f"Name: {rec_name}")
            print(f"Recorded: {local_started.isoformat()}")
            print(f"Original file size: {format_size(rec_file_size)}")
            print(f"Directory: {target_dir}")
            print(f"File: {file_to_save_as}")
            print("Downloading...")

            downloaded_bytes = 0
            last_update_time = time.monotonic()
            last_update_bytes = 0
            current_speed = 0
            progress_active = True
            print_progress(0, rec_file_size, 0)

            with requests.get(
                rec_url,
                stream=True,
                timeout=DOWNLOAD_TIMEOUT,
            ) as download_response:
                download_response.raise_for_status()
                with open(temp_path, "wb") as file_handle:
                    for chunk in download_response.iter_content(
                        chunk_size=CHUNK_SIZE
                    ):
                        if not chunk:
                            continue

                        file_handle.write(chunk)
                        downloaded_bytes += len(chunk)

                        now = time.monotonic()
                        elapsed = now - last_update_time
                        if elapsed >= PROGRESS_REFRESH_SECONDS:
                            current_speed = (
                                downloaded_bytes - last_update_bytes
                            ) / elapsed
                            print_progress(
                                downloaded_bytes,
                                rec_file_size,
                                current_speed,
                            )
                            last_update_time = now
                            last_update_bytes = downloaded_bytes

            now = time.monotonic()
            elapsed = now - last_update_time
            if elapsed > 0 and downloaded_bytes > last_update_bytes:
                current_speed = (
                    downloaded_bytes - last_update_bytes
                ) / elapsed
            print_progress(
                downloaded_bytes,
                rec_file_size,
                current_speed,
                final=True,
            )
            progress_active = False

            file_size = os.path.getsize(temp_path)
            print(
                "Download finished. "
                f"Saved {format_size(file_size)}. Verifying file size..."
            )

            if file_size != rec_file_size:
                os.remove(temp_path)
                print(
                    f"{RED}--- ERROR: Download failed, file size mismatch "
                    f"(expected {rec_file_size}, got {file_size}) ---{RESET}"
                )
                continue

            os.replace(temp_path, path)
            downloaded_count += 1
            print(f"{GREEN}Download Completed{RESET}")
            print("File size verified.")
            print("Deleting verified recording from ClickMeeting...")

            rec_del_resp = requests.delete(
                f"{API_BASE_URL}/conferences/{conference_id}/recordings/{rec_id}",
                headers=headers,
                timeout=API_TIMEOUT,
            )
            if rec_del_resp.ok:
                deleted_count += 1
                print(f"{GREEN}Record Deleted{RESET}")
            else:
                print(
                    f"{RED}--- ERROR: Unable to delete record "
                    f"(status code={rec_del_resp.status_code}) ---{RESET}"
                )

        except (
            KeyError,
            TypeError,
            ValueError,
            OSError,
            requests.RequestException,
        ) as exc:
            if progress_active:
                sys.stdout.write("\n")
                sys.stdout.flush()
            print(f"{RED}--- ERROR: Recording processing failed: {exc} ---{RESET}")
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

    print()
    print(
        f"Account completed: {account_name}. "
        f"Downloaded: {downloaded_count}/{len(recordings)}, "
        f"deleted from ClickMeeting: {deleted_count}/{len(recordings)}."
    )

print()
print("=" * 70)
print("Job done!")
print(
    "If you find this script useful, you can express your gratitude by supporting me "
    "with a coffee at https://www.buymeacoffee.com/slawciod82"
)
