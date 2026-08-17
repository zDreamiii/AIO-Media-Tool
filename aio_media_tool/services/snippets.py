from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterator


@dataclass(frozen=True, slots=True)
class Snippet:
    id: int
    kind: str
    preview: str
    text_content: str
    binary_content: bytes | None
    source_app: str
    created_at: str


class SnippetDatabase:
    TABLE = "AIO_M_Snippets"

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        with suppress(OSError):
            self.path.chmod(0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA secure_delete=ON")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
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
                f"""
                CREATE TABLE IF NOT EXISTS {self.TABLE} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL CHECK(kind IN ('text', 'image')),
                    preview TEXT NOT NULL,
                    text_content TEXT NOT NULL DEFAULT '',
                    binary_content BLOB,
                    content_hash TEXT NOT NULL,
                    source_app TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )
                """
            )
            db.execute(
                f"CREATE INDEX IF NOT EXISTS snippets_created ON {self.TABLE}(created_at DESC)"
            )
            db.execute(f"CREATE INDEX IF NOT EXISTS snippets_hash ON {self.TABLE}(content_hash)")

    def add_text(self, text: str, source_app: str = "") -> int | None:
        text = text.replace("\x00", "")
        if not text.strip() or len(text.encode("utf-8")) > 2 * 1024 * 1024:
            return None
        digest = hashlib.sha256(b"text\0" + text.encode("utf-8")).hexdigest()
        preview = " ".join(text.strip().split())[:240]
        return self._insert("text", preview, text, None, digest, source_app)

    def add_image(self, png_data: bytes, description: str, source_app: str = "") -> int | None:
        if not png_data or len(png_data) > 20 * 1024 * 1024:
            return None
        digest = hashlib.sha256(b"image\0" + png_data).hexdigest()
        return self._insert("image", description[:240], "", png_data, digest, source_app)

    def _insert(
        self,
        kind: str,
        preview: str,
        text_content: str,
        binary_content: bytes | None,
        digest: str,
        source_app: str,
    ) -> int | None:
        with self._connection() as db:
            latest = db.execute(
                f"SELECT content_hash FROM {self.TABLE} ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if latest and latest["content_hash"] == digest:
                return None
            cursor = db.execute(
                f"""
                INSERT INTO {self.TABLE}
                    (kind, preview, text_content, binary_content, content_hash, source_app, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    kind,
                    preview,
                    text_content,
                    binary_content,
                    digest,
                    source_app[:180],
                    datetime.now(UTC).isoformat(),
                ),
            )
            db.execute(
                f"""
                DELETE FROM {self.TABLE}
                WHERE id NOT IN (SELECT id FROM {self.TABLE} ORDER BY id DESC LIMIT 5000)
                """
            )
            return int(cursor.lastrowid)

    def recent(self, limit: int = 100, query: str = "") -> list[Snippet]:
        limit = max(1, min(500, int(limit)))
        with self._connection() as db:
            if query.strip():
                pattern = f"%{query.strip()}%"
                rows = db.execute(
                    f"""
                    SELECT * FROM {self.TABLE}
                    WHERE preview LIKE ? ESCAPE '\\' OR text_content LIKE ? ESCAPE '\\'
                    ORDER BY id DESC LIMIT ?
                    """,
                    (pattern, pattern, limit),
                ).fetchall()
            else:
                rows = db.execute(
                    f"SELECT * FROM {self.TABLE} ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
        return [self._from_row(row) for row in rows]

    def get(self, snippet_id: int) -> Snippet | None:
        with self._connection() as db:
            row = db.execute(
                f"SELECT * FROM {self.TABLE} WHERE id = ?", (int(snippet_id),)
            ).fetchone()
        return self._from_row(row) if row else None

    @staticmethod
    def _from_row(row: sqlite3.Row) -> Snippet:
        return Snippet(
            id=int(row["id"]),
            kind=str(row["kind"]),
            preview=str(row["preview"]),
            text_content=str(row["text_content"]),
            binary_content=row["binary_content"],
            source_app=str(row["source_app"]),
            created_at=str(row["created_at"]),
        )

    def delete_older_than(self, hours: int) -> int:
        cutoff = datetime.now(UTC) - timedelta(hours=max(1, int(hours)))
        with self._connection() as db:
            cursor = db.execute(
                f"DELETE FROM {self.TABLE} WHERE created_at < ?", (cutoff.isoformat(),)
            )
            count = max(0, cursor.rowcount)
            db.commit()
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            return count

    def delete(self, snippet_id: int) -> None:
        with self._connection() as db:
            db.execute(f"DELETE FROM {self.TABLE} WHERE id = ?", (int(snippet_id),))

    def clear(self) -> None:
        with self._connection() as db:
            db.execute(f"DELETE FROM {self.TABLE}")
            db.commit()
            db.execute("VACUUM")
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")


class ActiveApplicationDetector:
    @staticmethod
    def name() -> str:
        try:
            if sys.platform == "win32":
                return ActiveApplicationDetector._windows()
            if sys.platform == "darwin":
                result = subprocess.run(
                    [
                        "osascript",
                        "-e",
                        'tell application "System Events" to get name of first application process whose frontmost is true',
                    ],
                    capture_output=True,
                    text=True,
                    timeout=2,
                    check=False,
                )
                return result.stdout.strip()
            return ActiveApplicationDetector._linux()
        except (OSError, subprocess.SubprocessError, ValueError):
            return ""

    @staticmethod
    def _windows() -> str:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        window = user32.GetForegroundWindow()
        process_id = wintypes.DWORD()
        user32.GetWindowThreadProcessId(window, ctypes.byref(process_id))
        process = kernel32.OpenProcess(0x1000, False, process_id.value)
        if not process:
            return ""
        try:
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)):
                return Path(buffer.value).stem
        finally:
            kernel32.CloseHandle(process)
        return ""

    @staticmethod
    def _linux() -> str:
        xdotool = shutil.which("xdotool")
        if xdotool:
            result = subprocess.run(
                [xdotool, "getactivewindow", "getwindowpid"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip().isdigit():
                command = Path("/proc") / result.stdout.strip() / "comm"
                if command.is_file():
                    return command.read_text(encoding="utf-8", errors="replace").strip()
        return os.environ.get("XDG_CURRENT_DESKTOP", "")

    @staticmethod
    def is_blacklisted(app_name: str, blacklist: str) -> bool:
        normalized = app_name.casefold().strip()
        if not normalized:
            return False
        blocked = [value.casefold().strip() for value in blacklist.split(",") if value.strip()]
        return any(value in normalized for value in blocked)
