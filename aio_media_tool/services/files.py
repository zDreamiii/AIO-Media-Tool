from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Event
from uuid import uuid4

from aio_media_tool.services.common import ProgressCallback, check_cancelled, noop_progress


@dataclass(slots=True)
class RenameOptions:
    template: str = "{name}_{n}"
    start: int = 1
    padding: int = 3
    date_format: str = "%Y-%m-%d"
    date_source: str = "modified"
    regex_pattern: str = ""
    regex_replacement: str = ""
    extensions: str = ""
    recursive: bool = False


@dataclass(slots=True)
class RenamePreview:
    source: Path
    destination: Path
    error: str = ""


class BulkRenameService:
    """Previewable, collision-safe bulk rename with a two-phase commit."""

    INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
    TOKENS = {"name", "ext", "date", "datetime", "n", "parent"}

    @staticmethod
    def _date_for(path: Path, options: RenameOptions) -> datetime:
        stat = path.stat()
        if options.date_source == "created":
            timestamp = stat.st_ctime
        elif options.date_source == "now":
            return datetime.now().astimezone()
        else:
            timestamp = stat.st_mtime
        return datetime.fromtimestamp(timestamp).astimezone()

    @staticmethod
    def _extension_filter(value: str) -> set[str]:
        result: set[str] = set()
        for part in re.split(r"[,;\s]+", value.strip()):
            if not part:
                continue
            result.add((part if part.startswith(".") else f".{part}").casefold())
        return result

    def collect(self, folder: Path, options: RenameOptions) -> list[Path]:
        if not folder.is_dir():
            raise NotADirectoryError(folder)
        allowed = self._extension_filter(options.extensions)
        iterator = folder.rglob("*") if options.recursive else folder.iterdir()
        return sorted(
            (
                path
                for path in iterator
                if path.is_file() and (not allowed or path.suffix.casefold() in allowed)
            ),
            key=lambda path: str(path.relative_to(folder)).casefold(),
        )

    def preview(self, folder: Path, options: RenameOptions) -> list[RenamePreview]:
        if not options.template.strip():
            raise ValueError("Bitte ein Namensmuster angeben.")
        try:
            regex = re.compile(options.regex_pattern) if options.regex_pattern else None
        except re.error as exc:
            raise ValueError(f"Ungültiger regulärer Ausdruck: {exc}") from exc
        try:
            options.template.format(
                name="name", ext="ext", date="date", datetime="datetime", n=1, parent="ordner"
            )
        except (KeyError, ValueError) as exc:
            raise ValueError(
                "Unbekannter Platzhalter. Erlaubt: {name}, {ext}, {date}, {datetime}, {n}, {parent}."
            ) from exc

        sources = self.collect(folder, options)
        width = max(1, min(12, int(options.padding)))
        rows: list[RenamePreview] = []
        for offset, source in enumerate(sources):
            base = source.stem
            if regex:
                base = regex.sub(options.regex_replacement, base)
            moment = self._date_for(source, options)
            number = options.start + offset
            values = {
                "name": base,
                "ext": source.suffix.lstrip("."),
                "date": moment.strftime(options.date_format),
                "datetime": moment.strftime(f"{options.date_format}_%H-%M-%S"),
                "n": f"{number:0{width}d}",
                "parent": source.parent.name,
            }
            rendered = self.INVALID_CHARS.sub("_", options.template.format(**values)).strip(" .")
            if not rendered:
                rows.append(RenamePreview(source, source, "Der neue Dateiname wäre leer."))
                continue
            # {ext} may be part of the template; otherwise retain the original extension.
            suffix = source.suffix
            if "{ext}" in options.template:
                filename = rendered
                if not Path(filename).suffix and suffix:
                    filename = f"{filename}.{values['ext']}"
            else:
                filename = f"{rendered}{suffix}"
            rows.append(RenamePreview(source, source.with_name(filename)))

        source_rows = {os.path.normcase(str(row.source.resolve())): row for row in rows}
        source_keys = set(source_rows)
        target_counts: dict[str, int] = {}
        for row in rows:
            key = os.path.normcase(str(row.destination.resolve()))
            target_counts[key] = target_counts.get(key, 0) + 1
        for row in rows:
            key = os.path.normcase(str(row.destination.resolve()))
            if row.destination == row.source:
                row.error = row.error or "Unverändert"
            elif target_counts[key] > 1:
                row.error = "Mehrere Dateien hätten denselben Namen."
            elif row.destination.exists():
                occupying = source_rows.get(key)
                if key not in source_keys or (
                    occupying is not None and occupying.destination == occupying.source
                ):
                    row.error = "Zieldatei existiert bereits und wird nicht umbenannt."
        return rows

    def apply(
        self,
        folder: Path,
        options: RenameOptions,
        progress: ProgressCallback = noop_progress,
        cancel: Event | None = None,
    ) -> list[Path]:
        rows = self.preview(folder, options)
        actionable = [row for row in rows if row.destination != row.source]
        errors = [row for row in actionable if row.error]
        if errors:
            raise ValueError(f"Umbenennen abgebrochen: {errors[0].error}")
        if not actionable:
            raise ValueError("Das Muster würde keine Dateinamen verändern.")

        staged: list[tuple[Path, Path, Path]] = []
        completed: list[tuple[Path, Path, Path]] = []
        try:
            for index, row in enumerate(actionable):
                check_cancelled(cancel)
                temporary = row.source.with_name(f".aio-rename-{uuid4().hex}{row.source.suffix}")
                os.replace(row.source, temporary)
                staged.append((row.source, temporary, row.destination))
                progress(int((index + 1) / len(actionable) * 45), "Dateien werden vorbereitet")
            for index, triple in enumerate(staged):
                check_cancelled(cancel)
                source, temporary, destination = triple
                os.replace(temporary, destination)
                completed.append(triple)
                progress(45 + int((index + 1) / len(staged) * 55), destination.name)
        except Exception:
            # Recover both already-finalized and still-staged files as far as possible.
            for source, _temporary, destination in reversed(completed):
                if destination.exists() and not source.exists():
                    os.replace(destination, source)
            for source, temporary, _destination in reversed(staged):
                if temporary.exists() and not source.exists():
                    os.replace(temporary, source)
            raise
        return [row.destination for row in actionable]
