from __future__ import annotations

import csv
import json
import os
import shutil
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import Any

from PIL import ExifTags, Image
from pypdf import PdfReader, PdfWriter

from aio_media_tool.services.common import (
    ProgressCallback,
    check_cancelled,
    noop_progress,
    run_command,
    run_ffmpeg,
    unique_output,
)

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".mp4", ".pdf", ".mp3"}


@dataclass(slots=True)
class MetadataCleanResult:
    source: str
    output: str
    backup: str
    before: dict[str, str]
    after: dict[str, str]
    removed: dict[str, str]
    status: str = "bereinigt"


class MetadataCleanerService:
    def __init__(self, exiftool_path: str = "") -> None:
        self.exiftool = exiftool_path.strip() or shutil.which("exiftool") or ""

    @staticmethod
    def scan(folder: Path) -> list[Path]:
        if not folder.is_dir():
            raise NotADirectoryError(folder)
        ignored = {".bak", "AIO_M_Clean"}
        return sorted(
            (
                path
                for path in folder.rglob("*")
                if path.is_file()
                and not path.is_symlink()
                and path.suffix.casefold() in SUPPORTED_EXTENSIONS
                and not any(part in ignored for part in path.relative_to(folder).parts)
            ),
            key=lambda value: str(value).casefold(),
        )

    def read_metadata(self, path: Path) -> dict[str, str]:
        if self.exiftool:
            result = run_command([self.exiftool, "-j", "-G1", "-s", str(path)], timeout=45)
            if result.returncode == 0:
                try:
                    payload = json.loads(result.stdout)[0]
                    return self._filter_metadata(payload)
                except (IndexError, TypeError, ValueError, json.JSONDecodeError):
                    pass
        suffix = path.suffix.casefold()
        try:
            if suffix in {".jpg", ".jpeg", ".png"}:
                return self._image_metadata(path)
            if suffix == ".pdf":
                metadata = PdfReader(path).metadata or {}
                return {
                    str(key).lstrip("/"): str(value) for key, value in metadata.items() if value
                }
            if suffix == ".mp3":
                return self._mutagen_metadata(path)
            if suffix == ".mp4":
                return self._ffprobe_metadata(path)
        except Exception as exc:
            return {"Lesefehler": f"{type(exc).__name__}: {exc}"}
        return {}

    @staticmethod
    def _filter_metadata(payload: dict[str, Any]) -> dict[str, str]:
        excluded_prefixes = ("File:", "System:", "ExifTool:")
        excluded_names = {
            "SourceFile",
            "Directory",
            "FileName",
            "FileSize",
            "FilePermissions",
            "FileType",
            "FileTypeExtension",
            "MIMEType",
            "ImageWidth",
            "ImageHeight",
            "ImageSize",
            "Megapixels",
            "PDFVersion",
            "PageCount",
            "Linearized",
            "Duration",
            "VideoFrameRate",
        }
        result: dict[str, str] = {}
        for key, value in payload.items():
            if key.startswith(excluded_prefixes) or key.split(":")[-1] in excluded_names:
                continue
            if value in (None, "", [], {}):
                continue
            if isinstance(value, (dict, list)):
                result[str(key)] = json.dumps(value, ensure_ascii=False, sort_keys=True)
            else:
                result[str(key)] = str(value)
        return dict(sorted(result.items()))

    @staticmethod
    def _image_metadata(path: Path) -> dict[str, str]:
        result: dict[str, str] = {}
        with Image.open(path) as image:
            for key, value in image.getexif().items():
                result[str(ExifTags.TAGS.get(key, key))] = str(value)
            for key, value in image.info.items():
                if key.casefold() in {"exif", "icc_profile"}:
                    result[key] = f"{len(value)} Bytes" if isinstance(value, bytes) else str(value)
                elif isinstance(value, (str, int, float)):
                    result[key] = str(value)
        return dict(sorted(result.items()))

    @staticmethod
    def _mutagen_metadata(path: Path) -> dict[str, str]:
        import mutagen

        media = mutagen.File(path)
        if not media or not media.tags:
            return {}
        result: dict[str, str] = {}
        for key, value in media.tags.items():
            rendered = str(value)
            if key.startswith("APIC"):
                rendered = "Eingebettetes Bild"
            result[str(key)] = rendered[:500]
        return dict(sorted(result.items()))

    @staticmethod
    def _ffprobe_metadata(path: Path) -> dict[str, str]:
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            return {}
        result = run_command(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format_tags:stream_tags",
                "-of",
                "json",
                str(path),
            ],
            timeout=30,
        )
        if result.returncode:
            return {}
        raw = json.loads(result.stdout or "{}")
        output: dict[str, str] = {}
        for key, value in (raw.get("format", {}).get("tags") or {}).items():
            output[f"Format:{key}"] = str(value)
        for index, stream in enumerate(raw.get("streams", [])):
            for key, value in (stream.get("tags") or {}).items():
                output[f"Stream{index}:{key}"] = str(value)
        return dict(sorted(output.items()))

    def clean_batch(
        self,
        folder: Path,
        mode: str = "copy",
        output_dir: Path | None = None,
        progress: ProgressCallback = noop_progress,
        cancel: Event | None = None,
    ) -> list[Path]:
        sources = self.scan(folder)
        if not sources:
            raise ValueError("Im Ordner wurden keine unterstützten Dateien gefunden.")
        if mode not in {"copy", "backup"}:
            raise ValueError("Sicherheitsmodus muss 'copy' oder 'backup' sein.")
        clean_root = (output_dir or folder / "AIO_M_Clean").resolve()
        backup_root = (folder / ".bak").resolve()
        if mode == "copy":
            clean_root.mkdir(parents=True, exist_ok=True)
            sources = [
                source
                for source in sources
                if source.resolve() != clean_root and clean_root not in source.resolve().parents
            ]
            if not sources:
                raise ValueError("Außerhalb des Ausgabeordners wurden keine Quelldateien gefunden.")
        else:
            backup_root.mkdir(parents=True, exist_ok=True)
        results: list[MetadataCleanResult] = []
        for index, original in enumerate(sources):
            check_cancelled(cancel)
            relative = original.relative_to(folder)
            before = self.read_metadata(original)
            backup = Path()
            if mode == "backup":
                backup = backup_root / relative
                backup.parent.mkdir(parents=True, exist_ok=True)
                if backup.exists():
                    backup = unique_output(backup)
                shutil.move(str(original), str(backup))
                source_for_clean = backup
                target = original
            else:
                source_for_clean = original
                target = clean_root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target = unique_output(target)
            try:
                self._clean_to(source_for_clean, target, cancel)
            except Exception:
                if mode == "backup" and backup.exists() and not original.exists():
                    original.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(backup), str(original))
                raise
            after = self.read_metadata(target)
            removed = {key: value for key, value in before.items() if after.get(key) != value}
            results.append(
                MetadataCleanResult(
                    source=str(original),
                    output=str(target),
                    backup=str(backup) if mode == "backup" else "",
                    before=before,
                    after=after,
                    removed=removed,
                )
            )
            progress(
                int((index + 1) / len(sources) * 92),
                f"{target.name}: {len(removed)} Metadatenfelder entfernt",
            )
        log_root = clean_root if mode == "copy" else folder
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        json_log = log_root / f"AIO_M_metadata_clean_{stamp}.json"
        csv_log = log_root / f"AIO_M_metadata_clean_{stamp}.csv"
        payload = {
            "created_at": datetime.now(UTC).isoformat(),
            "mode": mode,
            "root": str(folder),
            "results": [asdict(item) for item in results],
        }
        json_log.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        with csv_log.open("w", newline="", encoding="utf-8-sig") as stream:
            writer = csv.writer(stream)
            writer.writerow(
                ["Quelle", "Ausgabe", "Backup", "Gelöschte Felder", "Vorher", "Nachher"]
            )
            for item in results:
                writer.writerow(
                    [
                        item.source,
                        item.output,
                        item.backup,
                        ", ".join(item.removed),
                        json.dumps(item.before, ensure_ascii=False, sort_keys=True),
                        json.dumps(item.after, ensure_ascii=False, sort_keys=True),
                    ]
                )
        progress(100, f"{len(results)} Dateien bereinigt")
        return [Path(item.output) for item in results] + [json_log, csv_log]

    def _clean_to(self, source: Path, target: Path, cancel: Event | None) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(
            prefix=f".{target.stem}-clean-", suffix=target.suffix, dir=target.parent
        )
        os.close(handle)
        temp = Path(temp_name)
        temp.unlink(missing_ok=True)
        try:
            suffix = source.suffix.casefold()
            if suffix in {".jpg", ".jpeg", ".png"}:
                self._clean_image(source, temp)
            elif suffix == ".pdf":
                self._clean_pdf(source, temp)
            elif suffix == ".mp3":
                self._clean_mp3(source, temp)
            elif suffix == ".mp4":
                self._clean_mp4(source, temp, cancel)
            else:
                raise ValueError(f"Nicht unterstützter Dateityp: {source.suffix}")
            check_cancelled(cancel)
            if self.exiftool:
                result = run_command(
                    [self.exiftool, "-overwrite_original", "-all=", str(temp)], timeout=120
                )
                if result.returncode:
                    raise RuntimeError(
                        result.stderr.strip() or "ExifTool-Bereinigung fehlgeschlagen"
                    )
            if not temp.is_file() or not temp.stat().st_size:
                raise RuntimeError(f"Für {source.name} wurde keine bereinigte Datei erzeugt.")
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)

    @staticmethod
    def _clean_image(source: Path, target: Path) -> None:
        with Image.open(source) as opened:
            image = opened.copy()
            image_format = opened.format or (
                "PNG" if source.suffix.casefold() == ".png" else "JPEG"
            )
        options: dict[str, Any] = {}
        if image_format.upper() == "JPEG":
            options.update(quality=95, optimize=True)
            if image.mode not in {"RGB", "L", "CMYK"}:
                image = image.convert("RGB")
        elif image_format.upper() == "PNG":
            options.update(optimize=True)
        image.save(target, format=image_format, **options)

    @staticmethod
    def _clean_pdf(source: Path, target: Path) -> None:
        reader = PdfReader(source)
        if reader.is_encrypted and reader.decrypt("") == 0:
            raise ValueError(
                f"{source.name} ist passwortgeschützt und kann nicht bereinigt werden."
            )
        writer = PdfWriter()
        for page in reader.pages:
            writer.add_page(page)
        writer.metadata = None
        with target.open("wb") as stream:
            writer.write(stream)

    @staticmethod
    def _clean_mp3(source: Path, target: Path) -> None:
        import mutagen

        shutil.copyfile(source, target)
        media = mutagen.File(target)
        if media is None:
            raise RuntimeError(f"{source.name} ist keine lesbare MP3-Datei.")
        if media.tags:
            media.delete()

    @staticmethod
    def _clean_mp4(source: Path, target: Path, cancel: Event | None) -> None:
        run_ffmpeg(
            [
                "-y",
                "-i",
                str(source),
                "-map",
                "0",
                "-map_metadata",
                "-1",
                "-map_metadata:s",
                "-1",
                "-map_chapters",
                "-1",
                "-fflags",
                "+bitexact",
                "-flags:v",
                "+bitexact",
                "-flags:a",
                "+bitexact",
                "-metadata",
                "encoder=",
                "-metadata:s:v",
                "encoder=",
                "-metadata:s:a",
                "encoder=",
                "-c",
                "copy",
                str(target),
            ],
            duration_seconds=None,
            cancel=cancel,
        )
