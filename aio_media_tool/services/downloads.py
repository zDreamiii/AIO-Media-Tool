from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any
from urllib.parse import parse_qs, urlparse

from aio_media_tool.models import JobCancelled
from aio_media_tool.services.common import (
    ProgressCallback,
    check_cancelled,
    noop_progress,
    validate_public_media_url,
)


@dataclass(slots=True)
class DownloadOptions:
    mode: str = "video"
    video_format: str = "mp4"
    max_height: int = 1080
    audio_format: str = "mp3"
    audio_quality: str = "320"
    playlist: bool = False
    subtitles: bool = False
    thumbnail: bool = True
    filename_template: str = "%(artist,uploader)s - %(title)s [%(id)s].%(ext)s"
    playlist_items: list[int] | None = None


class DownloadService:
    VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
    PLAYLIST_ID = re.compile(r"^(?:PL|UU|LL|RD|OLAK5uy_)[A-Za-z0-9_-]{10,}$")

    @staticmethod
    def _backend():
        try:
            import yt_dlp
        except ImportError as exc:
            raise RuntimeError("yt-dlp ist nicht installiert. Bitte `uv sync` ausführen.") from exc
        return yt_dlp

    @classmethod
    def normalize_input(cls, value: str) -> str:
        value = value.strip()
        if cls.VIDEO_ID.fullmatch(value):
            return f"https://www.youtube.com/watch?v={value}"
        if cls.PLAYLIST_ID.fullmatch(value):
            return f"https://www.youtube.com/playlist?list={value}"
        return validate_public_media_url(value)

    @classmethod
    def looks_like_playlist(cls, value: str) -> bool:
        value = value.strip()
        if cls.PLAYLIST_ID.fullmatch(value):
            return True
        parsed = urlparse(value)
        return bool(parse_qs(parsed.query).get("list")) or parsed.path.rstrip("/").endswith(
            "/playlist"
        )

    def inspect(self, url: str) -> dict[str, Any]:
        yt_dlp = self._backend()
        url = self.normalize_input(url)
        with yt_dlp.YoutubeDL(
            {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}
        ) as ydl:
            info = ydl.extract_info(url, download=False)
        return {
            "title": info.get("title") or "Unbekannt",
            "uploader": info.get("artist") or info.get("uploader") or "Unbekannt",
            "duration": info.get("duration"),
            "thumbnail": info.get("thumbnail"),
            "webpage_url": info.get("webpage_url") or url,
        }

    def inspect_collection(self, url: str) -> dict[str, Any]:
        """Return a flat, preview-friendly collection without downloading media."""
        yt_dlp = self._backend()
        url = self.normalize_input(url)
        options = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "extract_flat": "in_playlist",
            "lazy_playlist": False,
            "noplaylist": False,
            "ignoreerrors": True,
        }
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
        raw_entries = list(info.get("entries") or [info])
        entries: list[dict[str, Any]] = []
        for fallback_index, entry in enumerate(raw_entries, start=1):
            if not entry:
                continue
            media_id = str(entry.get("id") or "")
            webpage_url = entry.get("webpage_url") or entry.get("url") or ""
            if self.VIDEO_ID.fullmatch(str(webpage_url)):
                webpage_url = f"https://www.youtube.com/watch?v={webpage_url}"
            elif not str(webpage_url).startswith(("http://", "https://")) and media_id:
                webpage_url = f"https://www.youtube.com/watch?v={media_id}"
            entries.append(
                {
                    "index": int(entry.get("playlist_index") or fallback_index),
                    "id": media_id,
                    "title": entry.get("title") or f"Eintrag {fallback_index}",
                    "uploader": entry.get("artist") or entry.get("uploader") or "Unbekannt",
                    "duration": entry.get("duration"),
                    "thumbnail": entry.get("thumbnail") or "",
                    "webpage_url": webpage_url,
                }
            )
        if not entries:
            raise RuntimeError("Für diese Adresse wurden keine verfügbaren Einträge gefunden.")
        return {
            "title": info.get("title") or entries[0]["title"],
            "uploader": info.get("uploader") or entries[0]["uploader"],
            "webpage_url": info.get("webpage_url") or url,
            "is_playlist": bool(info.get("entries")),
            "entries": entries,
        }

    @staticmethod
    def _iter_entries(info: dict[str, Any]):
        entries = info.get("entries")
        if entries:
            for entry in entries:
                if entry:
                    yield from DownloadService._iter_entries(entry)
        else:
            yield info

    def download(
        self,
        url: str,
        output_dir: Path,
        options: DownloadOptions,
        progress: ProgressCallback = noop_progress,
        cancel: Event | None = None,
    ) -> list[Path]:
        yt_dlp = self._backend()
        url = self.normalize_input(url)
        output_dir.mkdir(parents=True, exist_ok=True)
        produced: list[Path] = []
        started_at = time.time() - 2

        def progress_hook(data: dict[str, Any]) -> None:
            check_cancelled(cancel)
            status = data.get("status")
            if status == "downloading":
                total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
                downloaded = data.get("downloaded_bytes") or 0
                value = int(downloaded / total * 90) if total else 5
                speed = data.get("_speed_str", "").strip()
                eta = data.get("_eta_str", "").strip()
                detail = " · ".join(part for part in (speed, f"noch {eta}" if eta else "") if part)
                progress(value, f"Download {detail}".strip())
            elif status == "finished":
                filename = data.get("filename")
                if filename:
                    produced.append(Path(filename))
                progress(92, "Download fertig, Nachbearbeitung läuft")

        def post_hook(data: dict[str, Any]) -> None:
            check_cancelled(cancel)
            info = data.get("info_dict") or {}
            filepath = info.get("filepath")
            if filepath:
                produced.append(Path(filepath))

        ydl_options: dict[str, Any] = {
            "outtmpl": str(output_dir / options.filename_template),
            "noplaylist": not options.playlist,
            "ignoreerrors": False,
            "continuedl": True,
            "overwrites": False,
            "windowsfilenames": True,
            "progress_hooks": [progress_hook],
            "postprocessor_hooks": [post_hook],
            "quiet": True,
            "no_warnings": True,
            "restrictfilenames": False,
        }
        if options.playlist_items:
            ydl_options["playlist_items"] = ",".join(str(value) for value in options.playlist_items)
            ydl_options["noplaylist"] = False
        if options.subtitles:
            ydl_options.update(
                {
                    "writesubtitles": True,
                    "writeautomaticsub": True,
                    "subtitleslangs": ["de", "en", "-live_chat"],
                }
            )
        if options.mode == "audio":
            codec = options.audio_format.lower()
            ydl_options.update(
                {
                    "format": "bestaudio/best",
                    "writethumbnail": options.thumbnail,
                    "postprocessors": [
                        {
                            "key": "FFmpegExtractAudio",
                            "preferredcodec": codec,
                            "preferredquality": options.audio_quality,
                        },
                        {"key": "FFmpegMetadata", "add_metadata": True},
                        *(
                            [{"key": "EmbedThumbnail"}]
                            if options.thumbnail and codec in {"mp3", "m4a"}
                            else []
                        ),
                    ],
                }
            )
        else:
            height = max(144, int(options.max_height or 2160))
            ydl_options.update(
                {
                    "format": f"bestvideo[height<={height}]+bestaudio/best[height<={height}]",
                    "merge_output_format": options.video_format.lower(),
                    "writethumbnail": options.thumbnail,
                    "postprocessors": [{"key": "FFmpegMetadata", "add_metadata": True}],
                }
            )
        try:
            with yt_dlp.YoutubeDL(ydl_options) as ydl:
                info = ydl.extract_info(url, download=True)
                for entry in self._iter_entries(info):
                    path = (
                        entry.get("requested_downloads", [{}])[0].get("filepath")
                        if entry.get("requested_downloads")
                        else None
                    )
                    if path:
                        produced.append(Path(path))
                    prepared = Path(ydl.prepare_filename(entry))
                    if options.mode == "audio":
                        prepared = prepared.with_suffix(f".{options.audio_format.lower()}")
                    produced.append(prepared)
        except Exception as exc:
            if cancel and cancel.is_set():
                raise JobCancelled("Download abgebrochen") from exc
            raise RuntimeError(str(exc)) from exc
        check_cancelled(cancel)
        media_suffixes = {".mp3", ".m4a", ".flac", ".wav", ".mp4", ".mkv", ".webm", ".opus", ".ogg"}
        existing = {
            path.resolve()
            for path in produced
            if path.exists() and path.suffix.lower() in media_suffixes
        }
        if not existing:
            existing = {
                path.resolve()
                for path in output_dir.iterdir()
                if path.is_file()
                and path.suffix.lower() in media_suffixes
                and path.stat().st_mtime >= started_at
            }
        if not existing:
            raise RuntimeError(
                "Der Download meldete Erfolg, aber es wurde keine Mediendatei gefunden."
            )
        progress(100, "Download abgeschlossen")
        return sorted(existing)
