from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import threading

from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import sessionmaker

from models import Account, Base, Event, Recording, Run, RuntimeWorker, SchedulerState


def utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class StateStore:
    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path).resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(
            f"sqlite+pysqlite:///{self.database_path}",
            future=True,
            connect_args={"timeout": 30},
        )
        self.Session = sessionmaker(self.engine, expire_on_commit=False)
        self._lock = threading.Lock()

    def initialize(self, interval_seconds: int) -> None:
        Base.metadata.create_all(self.engine)
        now = utc_now_naive()
        with self._lock, self.Session.begin() as session:
            scheduler = session.get(SchedulerState, 1)
            if scheduler is None:
                session.add(
                    SchedulerState(
                        id=1,
                        state="WAITING",
                        heartbeat_at=now,
                        interval_seconds=interval_seconds,
                    )
                )
            else:
                scheduler.interval_seconds = interval_seconds
                scheduler.heartbeat_at = now
            session.execute(delete(RuntimeWorker))

    def touch_heartbeat(self) -> None:
        with self._lock, self.Session.begin() as session:
            scheduler = session.get(SchedulerState, 1)
            if scheduler:
                scheduler.heartbeat_at = utc_now_naive()

    def start_run(self) -> int:
        now = utc_now_naive()
        with self._lock, self.Session.begin() as session:
            run = Run(status="RUNNING", started_at=now)
            session.add(run)
            session.flush()
            scheduler = session.get(SchedulerState, 1)
            if scheduler is None:
                raise RuntimeError("scheduler_state is not initialized")
            scheduler.state = "RUNNING"
            scheduler.current_run_id = run.id
            scheduler.next_run_at = None
            scheduler.heartbeat_at = now
            session.execute(delete(RuntimeWorker))
            return run.id

    def finish_run(
        self,
        run_id: int,
        recordings_found: int,
        downloaded_count: int,
        failed_count: int,
        next_run_at: datetime | None,
    ) -> str:
        status = "COMPLETED_WITH_ERRORS" if failed_count else "COMPLETED"
        now = utc_now_naive()
        with self._lock, self.Session.begin() as session:
            run = session.get(Run, run_id)
            if run is None:
                raise RuntimeError(f"Unknown run id {run_id}")
            run.status = status
            run.finished_at = now
            run.recordings_found = recordings_found
            run.downloaded_count = downloaded_count
            run.failed_count = failed_count
            scheduler = session.get(SchedulerState, 1)
            if scheduler:
                scheduler.state = "WAITING"
                scheduler.current_run_id = None
                scheduler.next_run_at = next_run_at
                scheduler.heartbeat_at = now
            session.execute(delete(RuntimeWorker))
        return status

    def fail_run(self, run_id: int, next_run_at: datetime | None) -> None:
        now = utc_now_naive()
        with self._lock, self.Session.begin() as session:
            run = session.get(Run, run_id)
            if run:
                run.status = "FAILED"
                run.finished_at = now
                run.failed_count = max(run.failed_count, 1)
            scheduler = session.get(SchedulerState, 1)
            if scheduler:
                scheduler.state = "WAITING"
                scheduler.current_run_id = None
                scheduler.next_run_at = next_run_at
                scheduler.heartbeat_at = now
            session.execute(delete(RuntimeWorker))

    def ensure_account(self, name: str) -> int:
        with self._lock, self.Session.begin() as session:
            account = session.scalar(select(Account).where(Account.name == name))
            if account is None:
                account = Account(name=name)
                session.add(account)
                session.flush()
            return account.id

    def upsert_recording(
        self,
        account_id: int,
        recording_id: str,
        conference_id: str,
        recording_name: str,
        recorded_at: datetime,
        expected_size: int,
    ) -> dict:
        with self._lock, self.Session.begin() as session:
            row = session.scalar(
                select(Recording).where(
                    Recording.account_id == account_id,
                    Recording.recording_id == recording_id,
                )
            )
            if row is None:
                row = Recording(
                    account_id=account_id,
                    recording_id=recording_id,
                    conference_id=conference_id,
                    recording_name=recording_name,
                    recorded_at=recorded_at,
                    expected_size=expected_size,
                    status="NEW",
                    attention_required=False,
                    attempt_count=0,
                    first_seen_at=utc_now_naive(),
                )
                session.add(row)
                session.flush()
            else:
                row.conference_id = conference_id
                row.recording_name = recording_name
                row.recorded_at = recorded_at
                row.expected_size = expected_size
            return {"id": row.id, "status": row.status, "local_path": row.local_path}

    def reconcile_account(self, account_id: int, remote_ids: set[str]) -> None:
        now = utc_now_naive()
        with self._lock, self.Session.begin() as session:
            rows = session.scalars(
                select(Recording).where(
                    Recording.account_id == account_id,
                    Recording.attention_required.is_(True),
                    Recording.status.in_(["QUARANTINED", "DELETE_FAILED"]),
                )
            ).all()
            for row in rows:
                if row.recording_id in remote_ids:
                    continue
                if row.status == "QUARANTINED":
                    row.status = "RESOLVED_EXTERNALLY"
                    row.resolution = "manual_external"
                    event_type = "RESOLVED_EXTERNALLY"
                    message = (
                        "Recording disappeared from ClickMeeting while quarantined. "
                        "Manual/external handling inferred."
                    )
                else:
                    row.status = "COMPLETED_MANUAL_DELETE"
                    row.resolution = "manual_delete"
                    event_type = "COMPLETED_MANUAL_DELETE"
                    message = (
                        "Verified local copy existed and the remote recording disappeared "
                        "from ClickMeeting. Remote deletion completed externally."
                    )
                row.attention_required = False
                row.completed_at = now
                session.add(
                    Event(
                        recording_id=row.id,
                        event_type=event_type,
                        message=message,
                        created_at=now,
                    )
                )

    def begin_download(self, recording_pk: int) -> None:
        now = utc_now_naive()
        with self._lock, self.Session.begin() as session:
            row = session.get(Recording, recording_pk)
            if row is None:
                raise RuntimeError(f"Unknown recording id {recording_pk}")
            row.status = "DOWNLOADING"
            row.attention_required = False
            row.last_error = None
            row.last_attempt_at = now
            row.attempt_count += 1

    def worker_start(
        self,
        worker_name: str,
        run_id: int,
        recording_pk: int,
        total_bytes: int,
    ) -> None:
        now = utc_now_naive()
        with self._lock, self.Session.begin() as session:
            session.execute(delete(RuntimeWorker).where(RuntimeWorker.worker_name == worker_name))
            session.add(
                RuntimeWorker(
                    worker_name=worker_name,
                    run_id=run_id,
                    recording_id=recording_pk,
                    state="DOWNLOADING",
                    downloaded_bytes=0,
                    total_bytes=total_bytes,
                    speed_bps=0,
                    started_at=now,
                    updated_at=now,
                )
            )

    def worker_progress(
        self,
        worker_name: str,
        downloaded_bytes: int,
        total_bytes: int,
        speed_bps: float,
    ) -> None:
        with self._lock, self.Session.begin() as session:
            worker = session.scalar(
                select(RuntimeWorker).where(RuntimeWorker.worker_name == worker_name)
            )
            if worker:
                worker.downloaded_bytes = downloaded_bytes
                worker.total_bytes = total_bytes
                worker.speed_bps = speed_bps
                worker.updated_at = utc_now_naive()

    def worker_stop(self, worker_name: str) -> None:
        with self._lock, self.Session.begin() as session:
            session.execute(delete(RuntimeWorker).where(RuntimeWorker.worker_name == worker_name))

    def mark_retryable(self, recording_pk: int, message: str) -> None:
        self._mark(recording_pk, "RETRYABLE_ERROR", False, message)

    def mark_quarantined(self, recording_pk: int, message: str) -> None:
        self._mark(recording_pk, "QUARANTINED", True, message)

    def mark_delete_failed(self, recording_pk: int, local_path: str, message: str) -> None:
        now = utc_now_naive()
        with self._lock, self.Session.begin() as session:
            row = session.get(Recording, recording_pk)
            if row is None:
                return
            first_failure = row.status != "DELETE_FAILED"
            row.status = "DELETE_FAILED"
            row.attention_required = True
            row.local_path = local_path
            row.last_error = message
            row.last_attempt_at = now
            if first_failure:
                session.add(
                    Event(
                        recording_id=recording_pk,
                        event_type="DELETE_FAILED",
                        message=(
                            "Local file verified, but ClickMeeting deletion failed. "
                            "Automatic redownload disabled; remote deletion will be retried."
                        ),
                        created_at=now,
                    )
                )

    def mark_completed(
        self,
        recording_pk: int,
        local_path: str,
        resolution: str = "automatic",
        message: str | None = None,
    ) -> None:
        now = utc_now_naive()
        with self._lock, self.Session.begin() as session:
            row = session.get(Recording, recording_pk)
            if row is None:
                return
            row.status = "COMPLETED"
            row.attention_required = False
            row.resolution = resolution
            row.local_path = local_path
            row.last_error = None
            row.completed_at = now
            row.last_attempt_at = now
            session.add(
                Event(
                    recording_id=recording_pk,
                    event_type="COMPLETED",
                    message=message
                    or "Recording downloaded, verified and deleted from ClickMeeting automatically.",
                    created_at=now,
                )
            )

    def delete_retry_ok(self, recording_pk: int) -> None:
        now = utc_now_naive()
        with self._lock, self.Session.begin() as session:
            row = session.get(Recording, recording_pk)
            if row is None:
                return
            row.status = "COMPLETED"
            row.attention_required = False
            row.resolution = "automatic_delete_retry"
            row.last_error = None
            row.completed_at = now
            row.last_attempt_at = now
            session.add(
                Event(
                    recording_id=recording_pk,
                    event_type="COMPLETED",
                    message=(
                        "Remote deletion retry completed. Verified local copy was kept; "
                        "the recording was not downloaded again."
                    ),
                    created_at=now,
                )
            )

    def delete_retry_failed(self, recording_pk: int, message: str) -> None:
        with self._lock, self.Session.begin() as session:
            row = session.get(Recording, recording_pk)
            if row:
                row.last_error = message
                row.last_attempt_at = utc_now_naive()

    def _mark(
        self,
        recording_pk: int,
        status: str,
        attention_required: bool,
        message: str,
    ) -> None:
        now = utc_now_naive()
        with self._lock, self.Session.begin() as session:
            row = session.get(Recording, recording_pk)
            if row is None:
                return
            row.status = status
            row.attention_required = attention_required
            row.last_error = message
            row.last_attempt_at = now
            session.add(
                Event(
                    recording_id=recording_pk,
                    event_type=status,
                    message=message,
                    created_at=now,
                )
            )
