from __future__ import annotations

import io
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from threading import Event

from aio_media_tool.services.common import (
    ProgressCallback,
    check_cancelled,
    noop_progress,
    run_ffmpeg,
    unique_output,
)
from aio_media_tool.services.video import VideoService


@dataclass(slots=True)
class AudioMetadata:
    title: str = ""
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    year: str = ""
    genre: str = ""
    track: str = ""
    source_url: str = ""
    lyrics: str = ""
    cover: Path | None = None


class AudioService:
    def _cover_bytes(self, path: Path) -> tuple[str, bytes]:
        try:
            from PIL import Image, ImageOps
        except ImportError as exc:
            raise RuntimeError("Pillow ist für Coverbilder erforderlich.") from exc
        with Image.open(path) as opened:
            image = ImageOps.fit(
                opened.convert("RGB"), (1200, 1200), method=Image.Resampling.LANCZOS
            )
            buffer = io.BytesIO()
            image.save(buffer, "JPEG", quality=90, optimize=True)
            return "image/jpeg", buffer.getvalue()

    def tag_mp3(self, path: Path, metadata: AudioMetadata) -> Path:
        try:
            from mutagen.id3 import APIC, ID3, TALB, TCON, TDRC, TIT2, TPE1, TPE2, TRCK, USLT, WOAS
            from mutagen.mp3 import MP3
        except ImportError as exc:
            raise RuntimeError("Mutagen ist nicht installiert. Bitte `uv sync` ausführen.") from exc
        try:
            audio = MP3(path, ID3=ID3)
            if audio.tags is None:
                audio.add_tags()
        except Exception as exc:
            raise RuntimeError(f"{path.name} ist keine gültige MP3-Datei.") from exc
        tags = audio.tags
        assert tags is not None
        mapping = (
            ("TIT2", TIT2, metadata.title),
            ("TPE1", TPE1, metadata.artist),
            ("TALB", TALB, metadata.album),
            ("TPE2", TPE2, metadata.album_artist),
            ("TDRC", TDRC, metadata.year),
            ("TCON", TCON, metadata.genre),
            ("TRCK", TRCK, metadata.track),
        )
        for key, frame_type, value in mapping:
            if value:
                tags.delall(key)
                tags.add(frame_type(encoding=3, text=value))
        if metadata.source_url:
            tags.delall("WOAS")
            tags.add(WOAS(url=metadata.source_url))
        if metadata.lyrics:
            tags.delall("USLT")
            tags.add(USLT(encoding=3, lang="deu", desc="Lyrics", text=metadata.lyrics))
        if metadata.cover:
            mime, data = self._cover_bytes(metadata.cover)
            tags.delall("APIC")
            tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
        audio.save(v2_version=3)
        return path

    def normalize_mp3(
        self,
        source: Path,
        output: Path,
        progress: ProgressCallback = noop_progress,
        cancel: Event | None = None,
    ) -> Path:
        duration = (
            float(VideoService().probe(source).get("format", {}).get("duration") or 0) or None
        )
        output = unique_output(output)
        handle, temp_name = tempfile.mkstemp(
            prefix=f".{output.stem}-", suffix=".mp3", dir=output.parent
        )
        os.close(handle)
        temp = Path(temp_name)
        try:
            run_ffmpeg(
                [
                    "-y",
                    "-i",
                    str(source),
                    "-vn",
                    "-af",
                    "loudnorm=I=-16:TP=-1.5:LRA=11",
                    "-c:a",
                    "libmp3lame",
                    "-q:a",
                    "2",
                    str(temp),
                ],
                duration_seconds=duration,
                progress=progress,
                cancel=cancel,
            )
            os.replace(temp, output)
        finally:
            temp.unlink(missing_ok=True)
        return output

    def process_local_mp3(
        self,
        source: Path,
        output_dir: Path,
        metadata: AudioMetadata,
        normalize: bool = False,
        progress: ProgressCallback = noop_progress,
        cancel: Event | None = None,
    ) -> list[Path]:
        check_cancelled(cancel)
        output = output_dir / f"{source.stem}_bearbeitet.mp3"
        if normalize:
            result = self.normalize_mp3(source, output, progress, cancel)
        else:
            result = unique_output(output)
            shutil.copy2(source, result)
            progress(75, "Audiodatei kopiert")
        check_cancelled(cancel)
        self.tag_mp3(result, metadata)
        progress(100, "Tags gespeichert")
        return [result]

    def compose_mp3(
        self,
        sources: list[Path],
        output_dir: Path,
        metadata: AudioMetadata,
        *,
        merge: bool = False,
        normalize: bool = False,
        output_name: str = "",
        progress: ProgressCallback = noop_progress,
        cancel: Event | None = None,
    ) -> list[Path]:
        if not sources:
            raise ValueError("Bitte mindestens eine MP3-Datei auswählen.")
        if any(not source.is_file() or source.suffix.casefold() != ".mp3" for source in sources):
            raise ValueError("Das MP3-Studio akzeptiert ausschließlich vorhandene MP3-Dateien.")
        if len(sources) == 1 and not merge:
            return self.process_local_mp3(
                sources[0], output_dir, metadata, normalize, progress, cancel
            )

        check_cancelled(cancel)
        output_dir.mkdir(parents=True, exist_ok=True)
        safe_name = "".join(
            character if character not in '<>:"/\\|?*' else "_"
            for character in (output_name.strip() or metadata.title or "MP3-Mix")
        ).strip(" .")
        output = unique_output(output_dir / f"{safe_name or 'MP3-Mix'}.mp3")
        durations = [
            float(VideoService().probe(source).get("format", {}).get("duration") or 0)
            for source in sources
        ]
        duration = sum(durations) or None
        handle, temp_name = tempfile.mkstemp(
            prefix=f".{output.stem}-", suffix=".mp3", dir=output.parent
        )
        os.close(handle)
        temp = Path(temp_name)
        list_handle, list_name = tempfile.mkstemp(
            prefix="aio-mp3-list-", suffix=".txt", dir=output.parent
        )
        try:
            with os.fdopen(list_handle, "w", encoding="utf-8", newline="\n") as stream:
                for source in sources:
                    escaped = str(source.resolve()).replace("'", "'\\''")
                    stream.write(f"file '{escaped}'\n")
            audio_filter = ["-af", "loudnorm=I=-16:TP=-1.5:LRA=11"] if normalize else []
            run_ffmpeg(
                [
                    "-y",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    list_name,
                    "-vn",
                    *audio_filter,
                    "-c:a",
                    "libmp3lame",
                    "-q:a",
                    "2",
                    str(temp),
                ],
                duration_seconds=duration,
                progress=progress,
                cancel=cancel,
            )
            check_cancelled(cancel)
            os.replace(temp, output)
        finally:
            temp.unlink(missing_ok=True)
            Path(list_name).unlink(missing_ok=True)
        self.tag_mp3(output, metadata)
        progress(100, "MP3-Mix und Tags gespeichert")
        return [output]
