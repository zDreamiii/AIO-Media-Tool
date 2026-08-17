from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path
from threading import Event

from aio_media_tool.services.common import (
    ProgressCallback,
    atomic_write_bytes,
    check_cancelled,
    noop_progress,
)


@dataclass(slots=True)
class ImageOptions:
    output_format: str = "original"
    quality: int = 82
    max_width: int = 0
    max_height: int = 0
    target_kb: int = 0
    preserve_metadata: bool = False


class ImageService:
    EXTENSIONS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "AVIF": ".avif"}

    @staticmethod
    def _pillow():
        try:
            from PIL import Image, ImageOps
        except ImportError as exc:
            raise RuntimeError("Pillow ist nicht installiert. Bitte `uv sync` ausführen.") from exc
        return Image, ImageOps

    @staticmethod
    def _normalise_format(source_format: str | None, requested: str) -> str:
        value = requested.upper()
        if value == "ORIGINAL":
            value = (source_format or "PNG").upper()
        if value == "JPG":
            value = "JPEG"
        if value not in ImageService.EXTENSIONS:
            raise ValueError(f"Nicht unterstütztes Ausgabeformat: {value}")
        return value

    def _encode(self, image, output_format: str, quality: int, metadata: dict[str, bytes]) -> bytes:
        Image, _ = self._pillow()
        current = image
        if output_format == "JPEG":
            if current.mode in {"RGBA", "LA"}:
                background = Image.new("RGB", current.size, "white")
                alpha = current.getchannel("A")
                background.paste(current.convert("RGB"), mask=alpha)
                current = background
            elif current.mode not in {"RGB", "L"}:
                current = current.convert("RGB")
        options: dict[str, object] = {"optimize": True}
        if output_format in {"JPEG", "WEBP", "AVIF"}:
            options["quality"] = max(1, min(100, quality))
        elif output_format == "PNG":
            options["compress_level"] = max(0, min(9, round((100 - quality) / 11)))
        options.update(metadata)
        buffer = io.BytesIO()
        try:
            current.save(buffer, format=output_format, **options)
        except (KeyError, ValueError) as exc:
            if output_format == "AVIF":
                raise RuntimeError(
                    "AVIF-Unterstützung fehlt. Bitte das optionale Paket `avif` installieren."
                ) from exc
            raise
        return buffer.getvalue()

    def process_one(
        self,
        source: Path,
        output_dir: Path,
        options: ImageOptions,
        progress: ProgressCallback = noop_progress,
        cancel: Event | None = None,
    ) -> Path:
        Image, ImageOps = self._pillow()
        check_cancelled(cancel)
        with Image.open(source) as opened:
            source_format = opened.format
            image = ImageOps.exif_transpose(opened)
            image.load()
            metadata: dict[str, bytes] = {}
            if options.preserve_metadata:
                exif = opened.getexif()
                if exif:
                    exif[274] = 1
                    metadata["exif"] = exif.tobytes()
                profile = opened.info.get("icc_profile")
                if profile:
                    metadata["icc_profile"] = profile
            max_width = options.max_width or image.width
            max_height = options.max_height or image.height
            if image.width > max_width or image.height > max_height:
                image.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)
            output_format = self._normalise_format(source_format, options.output_format)
            quality = options.quality
            data = self._encode(image, output_format, quality, metadata)
            target = options.target_kb * 1024
            if target and output_format in {"JPEG", "WEBP", "AVIF"} and len(data) > target:
                low, high = 15, max(15, quality)
                best = data
                for _ in range(7):
                    check_cancelled(cancel)
                    candidate_quality = (low + high) // 2
                    candidate = self._encode(image, output_format, candidate_quality, metadata)
                    if len(candidate) <= target:
                        best = candidate
                        low = candidate_quality + 1
                    else:
                        high = candidate_quality - 1
                data = best
                quality = max(15, high)
            scale_round = 0
            while target and len(data) > target and min(image.size) > 320 and scale_round < 5:
                check_cancelled(cancel)
                ratio = max(0.68, min(0.95, (target / len(data)) ** 0.5 * 0.96))
                size = (max(1, int(image.width * ratio)), max(1, int(image.height * ratio)))
                image = image.resize(size, Image.Resampling.LANCZOS)
                data = self._encode(image, output_format, quality, metadata)
                scale_round += 1
            progress(90, f"{source.name} wird gespeichert")
            suffix = self.EXTENSIONS[output_format]
            result = atomic_write_bytes(output_dir / f"{source.stem}_optimiert{suffix}", data)
            progress(100, f"{result.name} fertig")
            return result

    def process_many(
        self,
        sources: list[Path],
        output_dir: Path,
        options: ImageOptions,
        progress: ProgressCallback = noop_progress,
        cancel: Event | None = None,
    ) -> list[Path]:
        if not sources:
            raise ValueError("Bitte mindestens ein Bild auswählen.")
        results: list[Path] = []
        for index, source in enumerate(sources):
            check_cancelled(cancel)

            def item_progress(
                value: int,
                message: str,
                item_index: int = index,
                source_count: int = len(sources),
            ) -> None:
                total = int((item_index + value / 100) / source_count * 100)
                progress(total, message)

            results.append(self.process_one(source, output_dir, options, item_progress, cancel))
        return results
