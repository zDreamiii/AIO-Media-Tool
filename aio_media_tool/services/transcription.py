from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path
from threading import Event
from typing import Any

from aio_media_tool.models import JobCancelled
from aio_media_tool.services.common import (
    ProgressCallback,
    atomic_write_bytes,
    check_cancelled,
    noop_progress,
    run_ffmpeg,
    unique_output,
)


@dataclass(frozen=True, slots=True)
class SubtitleSegment:
    start: float
    end: float
    text: str


@dataclass(slots=True)
class TranscriptionOptions:
    model: str = "small"
    language: str = ""
    device: str = "auto"
    beam_size: int = 5
    vad_filter: bool = True
    offline_only: bool = True
    write_srt: bool = True
    write_vtt: bool = True
    burn_hardsubs: bool = False


class TranscriptionService:
    MODEL_NAMES = {
        "tiny": "tiny",
        "base": "base",
        "small": "small",
        "large": "large-v3",
        "large-v3": "large-v3",
    }

    @staticmethod
    def available() -> tuple[bool, str]:
        try:
            installed = find_spec("faster_whisper") is not None
        except (ImportError, OSError, ValueError) as exc:
            return False, f"faster-whisper nicht verfügbar ({type(exc).__name__})"
        if not installed:
            return False, "faster-whisper ist nicht installiert"
        return True, "faster-whisper ist installiert und noch nicht geladen"

    @staticmethod
    def _backend() -> Any:
        try:
            import faster_whisper
        except (ImportError, OSError) as exc:
            raise RuntimeError(
                "Das optionale Paket faster-whisper fehlt. Installiere es mit "
                "'uv sync --extra transcription'."
            ) from exc
        return faster_whisper

    @staticmethod
    def _device_and_compute(device: str) -> tuple[str, str]:
        requested = device.casefold()
        if requested in {"cpu", "cuda"}:
            return requested, "float16" if requested == "cuda" else "int8"
        try:
            import ctranslate2

            if ctranslate2.get_cuda_device_count() > 0:
                return "cuda", "float16"
        except (ImportError, OSError, RuntimeError):
            pass
        return "cpu", "int8"

    def transcribe(
        self,
        source: Path,
        output_dir: Path,
        model_cache: Path,
        options: TranscriptionOptions,
        progress: ProgressCallback = noop_progress,
        cancel: Event | None = None,
    ) -> list[Path]:
        if not source.is_file():
            raise FileNotFoundError(source)
        if not options.write_srt and not options.write_vtt and not options.burn_hardsubs:
            raise ValueError("Bitte mindestens ein Untertitelformat auswählen.")
        check_cancelled(cancel)
        backend = self._backend()
        model_name = self.MODEL_NAMES.get(options.model.casefold())
        if not model_name:
            raise ValueError(f"Unbekanntes Whisper-Modell: {options.model}")
        model_cache.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)
        device, compute_type = self._device_and_compute(options.device)
        progress(2, f"Whisper {model_name} wird auf {device.upper()} geladen")
        try:
            model = backend.WhisperModel(
                model_name,
                device=device,
                compute_type=compute_type,
                download_root=str(model_cache),
                local_files_only=options.offline_only,
            )
        except Exception as exc:
            if options.offline_only:
                raise RuntimeError(
                    f"Das Modell '{model_name}' ist noch nicht lokal vorhanden. "
                    "Deaktiviere einmalig 'Nur vorhandene Modelle (offline)', um es zu laden."
                ) from exc
            raise RuntimeError(f"Whisper-Modell konnte nicht geladen werden: {exc}") from exc
        check_cancelled(cancel)
        try:
            raw_segments, info = model.transcribe(
                str(source),
                language=options.language or None,
                beam_size=max(1, min(10, int(options.beam_size))),
                vad_filter=options.vad_filter,
            )
            duration = float(getattr(info, "duration", 0.0) or 0.0)
            detected = str(getattr(info, "language", "") or "unbekannt")
            segments: list[SubtitleSegment] = []
            for raw in raw_segments:
                check_cancelled(cancel)
                text = " ".join(str(raw.text).strip().split())
                if not text:
                    continue
                segment = SubtitleSegment(float(raw.start), float(raw.end), text)
                segments.append(segment)
                value = int(segment.end / duration * 70) if duration else min(69, 5 + len(segments))
                progress(max(5, min(70, value)), f"Sprache: {detected} · {len(segments)} Segmente")
        except JobCancelled:
            raise
        except Exception as exc:
            raise RuntimeError(f"Transkription fehlgeschlagen: {exc}") from exc
        if not segments:
            raise RuntimeError("Whisper hat keine Sprache erkannt.")
        outputs: list[Path] = []
        srt_path: Path | None = None
        if options.write_srt or options.burn_hardsubs:
            srt_path = atomic_write_bytes(
                output_dir / f"{source.stem}.srt",
                self.render_srt(segments).encode("utf-8"),
            )
            if options.write_srt:
                outputs.append(srt_path)
        if options.write_vtt:
            outputs.append(
                atomic_write_bytes(
                    output_dir / f"{source.stem}.vtt",
                    self.render_vtt(segments).encode("utf-8"),
                )
            )
        progress(75, f"{len(segments)} Untertitelsegmente exportiert")
        if options.burn_hardsubs:
            if source.suffix.casefold() not in {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v"}:
                raise ValueError("Hardsubs können nur in eine Videodatei eingebrannt werden.")
            assert srt_path is not None
            try:
                outputs.append(
                    self.burn_subtitles(
                        source,
                        srt_path,
                        output_dir,
                        lambda value, message: progress(75 + value // 4, message),
                        cancel,
                    )
                )
            finally:
                if not options.write_srt:
                    srt_path.unlink(missing_ok=True)
        progress(100, "Transkription abgeschlossen")
        return outputs

    @staticmethod
    def _timestamp(seconds: float, separator: str) -> str:
        milliseconds = max(0, round(float(seconds) * 1000))
        hours, remainder = divmod(milliseconds, 3_600_000)
        minutes, remainder = divmod(remainder, 60_000)
        secs, millis = divmod(remainder, 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}{separator}{millis:03d}"

    @classmethod
    def render_srt(cls, segments: list[SubtitleSegment]) -> str:
        blocks = []
        for index, segment in enumerate(segments, 1):
            blocks.append(
                f"{index}\n{cls._timestamp(segment.start, ',')} --> "
                f"{cls._timestamp(segment.end, ',')}\n{segment.text}"
            )
        return "\n\n".join(blocks) + "\n"

    @classmethod
    def render_vtt(cls, segments: list[SubtitleSegment]) -> str:
        blocks = ["WEBVTT"]
        for segment in segments:
            blocks.append(
                f"{cls._timestamp(segment.start, '.')} --> "
                f"{cls._timestamp(segment.end, '.')}\n{segment.text}"
            )
        return "\n\n".join(blocks) + "\n"

    @staticmethod
    def _subtitle_filter(path: Path) -> str:
        value = path.resolve().as_posix().replace("\\", "\\\\")
        value = value.replace(":", "\\:").replace("'", "\\'")
        value = value.replace(",", "\\,").replace("[", "\\[").replace("]", "\\]")
        return f"subtitles=filename='{value}'"

    def burn_subtitles(
        self,
        source: Path,
        subtitles: Path,
        output_dir: Path,
        progress: ProgressCallback = noop_progress,
        cancel: Event | None = None,
    ) -> Path:
        output = unique_output(output_dir / f"{source.stem}_hardsubs.mp4")
        handle, temp_name = tempfile.mkstemp(
            prefix=f".{output.stem}-", suffix=".mp4", dir=output.parent
        )
        os.close(handle)
        temp = Path(temp_name)
        try:
            run_ffmpeg(
                [
                    "-y",
                    "-i",
                    str(source),
                    "-vf",
                    self._subtitle_filter(subtitles),
                    "-c:v",
                    "libx264",
                    "-preset",
                    "medium",
                    "-crf",
                    "20",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    "-movflags",
                    "+faststart",
                    str(temp),
                ],
                duration_seconds=None,
                progress=progress,
                cancel=cancel,
            )
            check_cancelled(cancel)
            if not temp.is_file() or not temp.stat().st_size:
                raise RuntimeError("FFmpeg hat kein Hardsub-Video erzeugt.")
            os.replace(temp, output)
        finally:
            temp.unlink(missing_ok=True)
        return output
