from __future__ import annotations

import tempfile
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer

from aio_media_tool.database import HistoryDatabase
from aio_media_tool.jobs import JobManager
from aio_media_tool.models import JobKind, JobStatus


def test_background_job_updates_database() -> None:
    app = QCoreApplication.instance() or QCoreApplication([])
    with tempfile.TemporaryDirectory() as directory:
        database = HistoryDatabase(Path(directory) / "history.sqlite3")
        manager = JobManager(database)
        loop = QEventLoop()

        def runner(progress, _cancel):
            progress(50, "Halbzeit")
            return [Path(directory) / "result.dat"]

        job = manager.submit(JobKind.DIAGNOSTIC, "Test", "intern", directory, {}, runner)

        def finish(updated):
            if updated.id == job.id and updated.status == JobStatus.COMPLETED:
                loop.quit()

        manager.job_updated.connect(finish)
        QTimer.singleShot(3000, loop.quit)
        loop.exec()
        assert job.status == JobStatus.COMPLETED
        stored = database.recent(1)[0]
        assert stored.status == JobStatus.COMPLETED
        assert stored.progress == 100
        assert app is not None
