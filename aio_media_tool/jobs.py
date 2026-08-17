from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from threading import Event

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from aio_media_tool.database import HistoryDatabase
from aio_media_tool.models import JobCancelled, JobKind, JobRecord, JobStatus
from aio_media_tool.services.common import ProgressCallback

JobRunner = Callable[[ProgressCallback, Event], list[Path]]


class WorkerSignals(QObject):
    progress = Signal(int, str)
    success = Signal(object)
    failed = Signal(str)
    cancelled = Signal()


class JobWorker(QRunnable):
    def __init__(self, runner: JobRunner, cancel_event: Event) -> None:
        super().__init__()
        self.runner = runner
        self.cancel_event = cancel_event
        self.signals = WorkerSignals()

    @Slot()
    def run(self) -> None:
        try:
            if self.cancel_event.is_set():
                raise JobCancelled("Abgebrochen")
            outputs = self.runner(self.signals.progress.emit, self.cancel_event)
            self.signals.success.emit(outputs)
        except JobCancelled:
            self.signals.cancelled.emit()
        except Exception as exc:
            message = str(exc).strip() or type(exc).__name__
            self.signals.failed.emit(message)


class JobManager(QObject):
    job_added = Signal(object)
    job_updated = Signal(object)
    activity_changed = Signal(int)

    def __init__(
        self, database: HistoryDatabase, max_workers: int = 2, parent: QObject | None = None
    ) -> None:
        super().__init__(parent)
        self.database = database
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(max(1, max_workers))
        self.jobs: dict[str, JobRecord] = {}
        self._cancel_events: dict[str, Event] = {}
        self._workers: dict[str, JobWorker] = {}

    def set_max_workers(self, value: int) -> None:
        self.pool.setMaxThreadCount(max(1, value))

    def submit(
        self,
        kind: JobKind,
        label: str,
        source: str,
        destination: str,
        payload: dict,
        runner: JobRunner,
    ) -> JobRecord:
        job = JobRecord(
            kind=kind, label=label, source=source, destination=destination, payload=payload
        )
        self.jobs[job.id] = job
        cancel_event = Event()
        self._cancel_events[job.id] = cancel_event
        self.database.upsert(job)
        self.job_added.emit(job)
        worker = JobWorker(runner, cancel_event)
        worker.signals.progress.connect(
            lambda value, message, job_id=job.id: self._progress(job_id, value, message)
        )
        worker.signals.success.connect(
            lambda outputs, job_id=job.id: self._success(job_id, outputs)
        )
        worker.signals.failed.connect(lambda error, job_id=job.id: self._failed(job_id, error))
        worker.signals.cancelled.connect(lambda job_id=job.id: self._cancelled(job_id))
        self._workers[job.id] = worker
        job.status = JobStatus.RUNNING
        job.message = "Gestartet"
        job.touch()
        self.database.upsert(job)
        self.job_updated.emit(job)
        self.pool.start(worker)
        self.activity_changed.emit(self.active_count())
        return job

    def cancel(self, job_id: str) -> None:
        event = self._cancel_events.get(job_id)
        if event:
            event.set()
            job = self.jobs.get(job_id)
            if job and job.status == JobStatus.RUNNING:
                job.message = "Wird abgebrochen …"
                job.touch()
                self.database.upsert(job)
                self.job_updated.emit(job)

    def cancel_all(self) -> None:
        for event in self._cancel_events.values():
            event.set()

    def active_count(self) -> int:
        return sum(
            job.status in {JobStatus.QUEUED, JobStatus.RUNNING} for job in self.jobs.values()
        )

    def _progress(self, job_id: str, value: int, message: str) -> None:
        job = self.jobs[job_id]
        job.progress = max(job.progress, min(100, max(0, int(value))))
        job.message = message[:300]
        job.touch()
        self.database.upsert(job)
        self.job_updated.emit(job)

    def _success(self, job_id: str, outputs: list[Path]) -> None:
        job = self.jobs[job_id]
        job.status = JobStatus.COMPLETED
        job.progress = 100
        job.message = "Abgeschlossen"
        job.outputs = [str(path) for path in outputs]
        self._finish(job)

    def _failed(self, job_id: str, error: str) -> None:
        job = self.jobs[job_id]
        job.status = JobStatus.FAILED
        job.message = "Fehlgeschlagen"
        job.error = error[:4000]
        self._finish(job)

    def _cancelled(self, job_id: str) -> None:
        job = self.jobs[job_id]
        job.status = JobStatus.CANCELLED
        job.message = "Abgebrochen"
        self._finish(job)

    def _finish(self, job: JobRecord) -> None:
        job.touch()
        self.database.upsert(job)
        self.job_updated.emit(job)
        self._cancel_events.pop(job.id, None)
        self._workers.pop(job.id, None)
        self.activity_changed.emit(self.active_count())
