from __future__ import annotations

import os
import tempfile
from collections.abc import Iterable
from contextlib import suppress
from pathlib import Path
from threading import Event

from aio_media_tool.services.common import (
    ProgressCallback,
    check_cancelled,
    noop_progress,
    unique_output,
)


def _pypdf():
    try:
        from pypdf import PdfReader, PdfWriter
        from pypdf.errors import FileNotDecryptedError, PdfReadError
    except ImportError as exc:
        raise RuntimeError("pypdf ist nicht installiert. Bitte `uv sync` ausführen.") from exc
    return PdfReader, PdfWriter, FileNotDecryptedError, PdfReadError


def parse_page_range(expression: str, page_count: int) -> list[int]:
    """Convert a one-based range like ``1-3,5`` to zero-based page indices."""
    if page_count < 1:
        return []
    if not expression.strip():
        return list(range(page_count))
    result: list[int] = []
    for token in expression.replace(" ", "").split(","):
        if not token:
            continue
        try:
            if "-" in token:
                start_raw, end_raw = token.split("-", 1)
                if not start_raw or not end_raw:
                    raise ValueError
                start, end = int(start_raw), int(end_raw)
                if start > end:
                    start, end = end, start
                values = range(start, end + 1)
            else:
                values = (int(token),)
        except ValueError as exc:
            raise ValueError(f"Ungültiger Seitenbereich: {token}") from exc
        for page in values:
            if page < 1 or page > page_count:
                raise ValueError(f"Seite {page} liegt außerhalb von 1–{page_count}.")
            index = page - 1
            if index not in result:
                result.append(index)
    if not result:
        raise ValueError("Der Seitenbereich ist leer.")
    return result


def parse_page_groups(expression: str, page_count: int) -> list[list[int]]:
    if not expression.strip():
        return [[index] for index in range(page_count)]
    return [parse_page_range(group, page_count) for group in expression.split(";") if group.strip()]


class PdfService:
    def _reader(self, path: Path, password: str = ""):
        PdfReader, _, _, _ = _pypdf()
        reader = PdfReader(str(path))
        if reader.is_encrypted and (not password or not reader.decrypt(password)):
            raise ValueError(f"{path.name} ist verschlüsselt. Das richtige Passwort wird benötigt.")
        return reader

    @staticmethod
    def _write(writer, output: Path) -> Path:
        output = unique_output(output)
        handle, temp_name = tempfile.mkstemp(
            prefix=f".{output.stem}-", suffix=".pdf", dir=output.parent
        )
        try:
            with os.fdopen(handle, "wb") as stream:
                writer.write(stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, output)
        finally:
            Path(temp_name).unlink(missing_ok=True)
        return output

    def merge(
        self,
        inputs: Iterable[Path],
        output: Path,
        progress: ProgressCallback = noop_progress,
        cancel: Event | None = None,
    ) -> list[Path]:
        _, PdfWriter, _, _ = _pypdf()
        files = [Path(path) for path in inputs]
        if len(files) < 2:
            raise ValueError("Zum Zusammenführen werden mindestens zwei PDFs benötigt.")
        if output.resolve() in {path.resolve() for path in files}:
            raise ValueError("Die Ausgabedatei darf keine Eingabedatei überschreiben.")
        writer = PdfWriter()
        total = len(files)
        for number, path in enumerate(files, 1):
            check_cancelled(cancel)
            reader = self._reader(path)
            for page in reader.pages:
                writer.add_page(page)
            progress(int(number / total * 90), f"{path.name} hinzugefügt")
        result = self._write(writer, output)
        progress(100, "PDF zusammengeführt")
        return [result]

    def split(
        self,
        source: Path,
        output_dir: Path,
        groups: str = "",
        progress: ProgressCallback = noop_progress,
        cancel: Event | None = None,
    ) -> list[Path]:
        _, PdfWriter, _, _ = _pypdf()
        reader = self._reader(source)
        page_groups = parse_page_groups(groups, len(reader.pages))
        outputs: list[Path] = []
        for number, indices in enumerate(page_groups, 1):
            check_cancelled(cancel)
            writer = PdfWriter()
            for index in indices:
                writer.add_page(reader.pages[index])
            suffix = (
                f"seiten_{indices[0] + 1}-{indices[-1] + 1}"
                if len(indices) > 1
                else f"seite_{indices[0] + 1}"
            )
            outputs.append(self._write(writer, output_dir / f"{source.stem}_{suffix}.pdf"))
            progress(int(number / len(page_groups) * 100), f"Gruppe {number} gespeichert")
        return outputs

    def extract(
        self,
        source: Path,
        output: Path,
        pages: str,
        progress: ProgressCallback = noop_progress,
        cancel: Event | None = None,
    ) -> list[Path]:
        _, PdfWriter, _, _ = _pypdf()
        reader = self._reader(source)
        indices = parse_page_range(pages, len(reader.pages))
        writer = PdfWriter()
        for number, index in enumerate(indices, 1):
            check_cancelled(cancel)
            writer.add_page(reader.pages[index])
            progress(int(number / len(indices) * 90), f"Seite {index + 1} extrahiert")
        result = self._write(writer, output)
        progress(100, "Auszug gespeichert")
        return [result]

    def rotate(
        self,
        source: Path,
        output: Path,
        degrees: int,
        pages: str = "",
        progress: ProgressCallback = noop_progress,
        cancel: Event | None = None,
    ) -> list[Path]:
        _, PdfWriter, _, _ = _pypdf()
        if degrees not in {90, 180, 270}:
            raise ValueError("Die Drehung muss 90, 180 oder 270 Grad betragen.")
        reader = self._reader(source)
        selected = set(parse_page_range(pages, len(reader.pages)))
        writer = PdfWriter()
        for index, page in enumerate(reader.pages):
            check_cancelled(cancel)
            if index in selected:
                page.rotate(degrees)
            writer.add_page(page)
            progress(int((index + 1) / len(reader.pages) * 90), f"Seite {index + 1} verarbeitet")
        result = self._write(writer, output)
        progress(100, "Drehung gespeichert")
        return [result]

    def compress(
        self,
        source: Path,
        output: Path,
        remove_metadata: bool = True,
        progress: ProgressCallback = noop_progress,
        cancel: Event | None = None,
    ) -> list[Path]:
        _, PdfWriter, _, _ = _pypdf()
        reader = self._reader(source)
        writer = PdfWriter()
        for index, page in enumerate(reader.pages):
            check_cancelled(cancel)
            with suppress(Exception):
                page.compress_content_streams()
            writer.add_page(page)
            progress(int((index + 1) / len(reader.pages) * 90), f"Seite {index + 1} komprimiert")
        if not remove_metadata and reader.metadata:
            metadata = {
                str(key): str(value) for key, value in reader.metadata.items() if value is not None
            }
            writer.add_metadata(metadata)
        result = self._write(writer, output)
        progress(100, "PDF optimiert")
        return [result]

    def protect(
        self,
        source: Path,
        output: Path,
        password: str,
        progress: ProgressCallback = noop_progress,
        cancel: Event | None = None,
    ) -> list[Path]:
        _, PdfWriter, _, _ = _pypdf()
        if len(password) < 4:
            raise ValueError("Bitte ein Passwort mit mindestens vier Zeichen wählen.")
        reader = self._reader(source)
        writer = PdfWriter()
        for page in reader.pages:
            check_cancelled(cancel)
            writer.add_page(page)
        writer.encrypt(password, algorithm="AES-256")
        result = self._write(writer, output)
        progress(100, "PDF geschützt")
        return [result]

    def unlock(
        self,
        source: Path,
        output: Path,
        password: str,
        progress: ProgressCallback = noop_progress,
        cancel: Event | None = None,
    ) -> list[Path]:
        _, PdfWriter, _, _ = _pypdf()
        reader = self._reader(source, password)
        writer = PdfWriter()
        for page in reader.pages:
            check_cancelled(cancel)
            writer.add_page(page)
        result = self._write(writer, output)
        progress(100, "PDF entsperrt")
        return [result]

    def set_metadata(
        self,
        source: Path,
        output: Path,
        *,
        title: str = "",
        author: str = "",
        subject: str = "",
        keywords: str = "",
        progress: ProgressCallback = noop_progress,
        cancel: Event | None = None,
    ) -> list[Path]:
        _, PdfWriter, _, _ = _pypdf()
        reader = self._reader(source)
        writer = PdfWriter()
        for index, page in enumerate(reader.pages):
            check_cancelled(cancel)
            writer.add_page(page)
            progress(int((index + 1) / len(reader.pages) * 80), f"Seite {index + 1} übernommen")
        metadata = {
            str(key): str(value)
            for key, value in (reader.metadata or {}).items()
            if value is not None
        }
        updates = {"/Title": title, "/Author": author, "/Subject": subject, "/Keywords": keywords}
        for key, value in updates.items():
            if value:
                metadata[key] = value
        if metadata:
            writer.add_metadata(metadata)
        result = self._write(writer, output)
        progress(100, "Dokumentinformationen gespeichert")
        return [result]
