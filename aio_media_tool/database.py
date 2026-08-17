from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from aio_media_tool.models import JobKind, JobRecord, JobStatus


class HistoryDatabase:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Open a database connection and always release its file handle.

        sqlite3.Connection's own context manager commits or rolls back, but it
        does not close the connection.  That matters on Windows where an open
        handle prevents temporary databases from being deleted.
        """
        db = self._connect()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _initialize(self) -> None:
        with self._connection() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    label TEXT NOT NULL,
                    source TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    progress INTEGER NOT NULL,
                    message TEXT NOT NULL,
                    outputs TEXT NOT NULL,
                    error TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            db.execute("CREATE INDEX IF NOT EXISTS jobs_updated ON jobs(updated_at DESC)")

    def upsert(self, job: JobRecord) -> None:
        data = job.to_dict()
        with self._connection() as db:
            db.execute(
                """
                INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status=excluded.status,
                    progress=excluded.progress,
                    message=excluded.message,
                    outputs=excluded.outputs,
                    error=excluded.error,
                    updated_at=excluded.updated_at
                """,
                (
                    data["id"],
                    data["kind"],
                    data["label"],
                    data["source"],
                    data["destination"],
                    json.dumps(data["payload"], ensure_ascii=False),
                    data["status"],
                    data["progress"],
                    data["message"],
                    json.dumps(data["outputs"], ensure_ascii=False),
                    data["error"],
                    data["created_at"],
                    data["updated_at"],
                ),
            )

    def recent(self, limit: int = 250) -> list[JobRecord]:
        with self._connection() as db:
            rows = db.execute(
                "SELECT * FROM jobs ORDER BY updated_at DESC LIMIT ?", (max(1, min(limit, 1000)),)
            ).fetchall()
        records: list[JobRecord] = []
        for row in rows:
            try:
                records.append(
                    JobRecord(
                        id=row["id"],
                        kind=JobKind(row["kind"]),
                        label=row["label"],
                        source=row["source"],
                        destination=row["destination"],
                        payload=json.loads(row["payload"]),
                        status=JobStatus(row["status"]),
                        progress=row["progress"],
                        message=row["message"],
                        outputs=json.loads(row["outputs"]),
                        error=row["error"],
                        created_at=row["created_at"],
                        updated_at=row["updated_at"],
                    )
                )
            except (ValueError, TypeError, json.JSONDecodeError):
                continue
        return records

    def clear_finished(self) -> None:
        finished = (
            JobStatus.COMPLETED.value,
            JobStatus.COMPLETED_WITH_WARNINGS.value,
            JobStatus.FAILED.value,
            JobStatus.CANCELLED.value,
        )
        with self._connection() as db:
            db.execute("DELETE FROM jobs WHERE status IN (?, ?, ?, ?)", finished)
