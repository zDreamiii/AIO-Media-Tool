from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Event, Lock

from aio_media_tool.services.common import (
    ProgressCallback,
    check_cancelled,
    noop_progress,
    require_executable,
    run_command,
    run_ffmpeg,
    unique_output,
)


def build_cut_segments(
    processing_start: float, processing_end: float, cut_markers: list[float]
) -> list[tuple[float, float]]:
    """Build ordered segment bounds inside a processing window.

    Markers outside the window are ignored. The final segment always ends at
    ``processing_end`` so media after that point is never exported.
    """
    start = max(0.0, float(processing_start))
    end = float(processing_end)
    if end <= start:
        return []
    markers = sorted(
        {
            round(float(marker), 3)
            for marker in cut_markers
            if start + 0.001 < float(marker) < end - 0.001
        }
    )
    boundaries = [start, *markers, end]
    return [
        (left, right)
        for left, right in zip(boundaries, boundaries[1:])
        if right - left >= 0.001
    ]



def parse_timecode(value: str) -> float:
    """Parse seconds or HH:MM:SS(.mmm) / MM:SS(.mmm) into seconds."""
    text = str(value).strip().replace(",", ".")
    if not text:
        raise ValueError("Bitte eine Zeit eingeben, z. B. 00:01:30.000.")
    parts = text.split(":")
    if len(parts) > 3:
        raise ValueError(f"Ungültiger Zeitwert: {value}")
    try:
        numbers = [float(part) for part in parts]
    except ValueError as exc:
        raise ValueError(f"Ungültiger Zeitwert: {value}") from exc
    if any(number < 0 for number in numbers):
        raise ValueError("Zeitwerte dürfen nicht negativ sein.")
    if len(numbers) == 1:
        return numbers[0]
    if len(numbers) == 2:
        minutes, seconds = numbers
        if seconds >= 60:
            raise ValueError("Sekunden müssen kleiner als 60 sein.")
        return minutes * 60 + seconds
    hours, minutes, seconds = numbers
    if minutes >= 60 or seconds >= 60:
        raise ValueError("Minuten und Sekunden müssen kleiner als 60 sein.")
    return hours * 3600 + minutes * 60 + seconds


def normalize_explicit_segments(
    segments: list[tuple[float, float]], full_duration: float = 0.0
) -> list[tuple[float, float]]:
    """Validate and sort explicit keep-ranges; gaps are intentionally discarded."""
    normalized: list[tuple[float, float]] = []
    maximum = max(0.0, float(full_duration))
    for raw_start, raw_end in segments:
        start = max(0.0, float(raw_start))
        end = float(raw_end)
        if maximum and start >= maximum:
            raise ValueError("Ein Segment startet hinter dem Videoende.")
        if maximum and end > maximum + 0.001:
            raise ValueError("Ein Segment endet hinter dem Videoende.")
        if end <= start:
            raise ValueError("Bei jedem Segment muss Ende nach Start liegen.")
        normalized.append((round(start, 6), round(end, 6)))
    normalized.sort(key=lambda item: (item[0], item[1]))
    for previous, current in zip(normalized, normalized[1:]):
        if current[0] < previous[1] - 0.0005:
            raise ValueError("Segmentbereiche dürfen sich nicht überlappen.")
    return normalized

def numbered_segment_name(base_name: str, segment_number: int) -> str:
    """Append a 1-based segment number without a separator.

    Example: ``Video 1`` becomes ``Video 11``, ``Video 12`` and so on.
    """
    base = base_name.strip() or "Segment"
    number = max(1, int(segment_number))
    return f"{base}{number}"


@dataclass(slots=True)
class VideoOptions:
    container: str = "mp4"
    codec: str = "h264"
    preset: str = "balanced"
    crf: int = 23
    height: int = 0
    fps: int = 0
    audio_bitrate: int = 160
    target_mb: int = 0
    mute: bool = False
    rotation: int = 0
    cpu_limit_percent: int = 100
    cpu_mode: str = "all"
    encoder_backend: str = "software"
    nvenc_mode: str = "quality"


HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}


@dataclass(frozen=True, slots=True)
class HdrInfo:
    is_hdr: bool
    transfer: str = ""
    primaries: str = ""
    colorspace: str = ""
    color_range: str = ""
    pixel_format: str = ""

    @property
    def label(self) -> str:
        if self.transfer == "smpte2084":
            return "HDR10 / PQ"
        if self.transfer == "arib-std-b67":
            return "HLG"
        return "HDR" if self.is_hdr else "SDR / unbekannt"


@dataclass(frozen=True, slots=True)
class VideoCompressionProfile:
    key: str
    name: str
    description: str
    container: str
    codec: str
    preset: str
    crf: int
    height: int
    fps: int
    target_mb: int
    audio_bitrate: int


VIDEO_COMPRESSION_PROFILES: tuple[VideoCompressionProfile, ...] = (
    VideoCompressionProfile(
        key="classic_cartoon",
        name="Klassischer TV-Zeichentrick",
        description=(
            "Flache 2D-Animation / klassische SD-TV-Quellen. AV1, Qualitätswert 25 (CRF/CQ), max. 1080p "
            "ohne Hochskalierung, Original-FPS, 160 kbit/s Audio."
        ),
        container="mp4",
        codec="av1",
        preset="small",
        crf=25,
        height=1080,
        fps=0,
        target_mb=0,
        audio_bitrate=160,
    ),
    VideoCompressionProfile(
        key="cinema_4k_imax",
        name="Echter 4K- / IMAX-Kinofilm",
        description=(
            "Detailreiche 4K-Realfilme mit Filmkorn. HEVC, Qualitätswert 19 (CRF/CQ), max. 2160p, "
            "24 fps und 320 kbit/s Audio."
        ),
        container="mkv",
        codec="h265",
        preset="quality",
        crf=19,
        height=2160,
        fps=24,
        target_mb=0,
        audio_bitrate=320,
    ),
    VideoCompressionProfile(
        key="modern_live_action",
        name="Moderner Realfilm / Serienstandard",
        description=(
            "Allround-Profil für Serien und Filme in Full HD. HEVC, Qualitätswert 22 (CRF/CQ), max. 1080p, "
            "Original-FPS und 192 kbit/s Audio."
        ),
        container="mp4",
        codec="h265",
        preset="balanced",
        crf=22,
        height=1080,
        fps=0,
        target_mb=0,
        audio_bitrate=192,
    ),
    VideoCompressionProfile(
        key="modern_cgi",
        name="Moderner 3D-/CGI-Animationsfilm",
        description=(
            "Saubere digital gerenderte Animation. AV1, Qualitätswert 23 (CRF/CQ), max. 2160p ohne "
            "Hochskalierung, Original-FPS und 256 kbit/s Audio."
        ),
        container="mp4",
        codec="av1",
        preset="small",
        crf=23,
        height=2160,
        fps=0,
        target_mb=0,
        audio_bitrate=256,
    ),
    VideoCompressionProfile(
        key="social_chat",
        name="Social Media / Chat-Clip",
        description=(
            "Kompatibler H.264-Upload mit fester Zielgröße. 720p, 30 fps, 128 kbit/s Audio; "
            "standardmäßig 25 MB. Für Discord/WhatsApp kann direkt 10, 16 oder 25 MB gewählt werden."
        ),
        container="mp4",
        codec="h264",
        preset="balanced",
        crf=23,
        height=720,
        fps=30,
        target_mb=25,
        audio_bitrate=128,
    ),
)


@dataclass(slots=True)
class GifOptions:
    start_seconds: float = 0
    end_seconds: float = 5
    fps: int = 12
    width: int = 720
    colors: int = 192
    loop: int = 0
    cpu_limit_percent: int = 100
    cpu_mode: str = "all"


@dataclass(slots=True)
class CutOptions:
    start_seconds: float = 0
    end_seconds: float = 5
    output_name: str = ""
    cpu_limit_percent: int = 100
    cpu_mode: str = "all"


class VideoService:
    CODECS = {
        "h264": "libx264",
        "h265": "libx265",
        "vp9": "libvpx-vp9",
        "av1": "libsvtav1",
    }
    NVENC_CODECS = {
        "h264": "h264_nvenc",
        "h265": "hevc_nvenc",
        "av1": "av1_nvenc",
    }
    NVENC_PRESETS = {
        "fast": "p3",
        "balanced": "p4",
        "small": "p5",
        "quality": "p6",
    }
    _nvenc_encoder_help: dict[str, str] = {}
    _nvenc_recording_lock = Lock()
    PRESETS = {
        "fast": {"libx264": "veryfast", "libx265": "veryfast", "libsvtav1": "10"},
        "balanced": {"libx264": "medium", "libx265": "medium", "libsvtav1": "8"},
        "small": {"libx264": "slow", "libx265": "slow", "libsvtav1": "6"},
        "quality": {"libx264": "slower", "libx265": "slower", "libsvtav1": "5"},
    }

    _hdr_filters_checked = False

    @classmethod
    def _nvenc_help(cls, encoder: str) -> str:
        cached = cls._nvenc_encoder_help.get(encoder)
        if cached is not None:
            return cached
        ffmpeg = require_executable("ffmpeg")
        result = run_command([ffmpeg, "-hide_banner", "-h", f"encoder={encoder}"], timeout=20)
        text = f"{result.stdout}\n{result.stderr}"
        if result.returncode or "Encoder " not in text:
            raise RuntimeError(
                f"FFmpeg stellt {encoder} nicht bereit. Bitte eine FFmpeg-Build mit NVIDIA NVENC "
                "verwenden und einen aktuellen NVIDIA-Treiber installieren."
            )
        cls._nvenc_encoder_help[encoder] = text
        return text

    @classmethod
    def _resolve_encoder(cls, options: VideoOptions) -> tuple[str, bool]:
        codec_key = str(options.codec or "h264").casefold()
        backend = str(options.encoder_backend or "software").casefold()
        if backend == "software":
            codec = cls.CODECS.get(codec_key)
            if not codec:
                raise ValueError(f"Unbekannter Videocodec: {options.codec}")
            return codec, False
        if backend != "nvenc":
            raise ValueError(f"Unbekannte Encoder-Engine: {options.encoder_backend}")
        codec = cls.NVENC_CODECS.get(codec_key)
        if not codec:
            if codec_key == "vp9":
                raise ValueError(
                    "VP9 wird von NVIDIA NVENC nicht unterstützt. Bitte AV1 verwenden oder "
                    "die Encoder-Engine auf CPU / Software stellen."
                )
            raise ValueError(f"Unbekannter NVENC-Videocodec: {options.codec}")
        cls._nvenc_help(codec)
        return codec, True

    @classmethod
    def _nvenc_tuning_args(cls, encoder: str, options: VideoOptions) -> list[str]:
        """Build NVENC quality/performance flags for normal and recording-safe use."""
        help_text = cls._nvenc_help(encoder)
        mode = str(options.nvenc_mode or "quality").casefold()
        if mode not in {"quality", "recording"}:
            raise ValueError(f"Unbekannter NVENC-Modus: {options.nvenc_mode}")
        preset = (
            "p4"
            if mode == "recording"
            else cls.NVENC_PRESETS.get(str(options.preset or "balanced"), "p4")
        )
        args = ["-preset", preset]
        if "-tune" in help_text:
            args.extend(["-tune", "hq"])
        if "-multipass" in help_text:
            if mode == "recording":
                multipass = "disabled" if "disabled" in help_text else "0"
            else:
                multipass = "qres" if "qres" in help_text else "1"
            args.extend(["-multipass", multipass])
        if mode == "recording":
            if "-rc-lookahead" in help_text:
                args.extend(["-rc-lookahead", "0"])
            if "-spatial-aq" in help_text:
                args.extend(["-spatial-aq", "0"])
            if "-temporal-aq" in help_text:
                args.extend(["-temporal-aq", "0"])
            # Ada and newer GPUs can split one HEVC/AV1 frame across multiple NVENC
            # engines. Disabling that keeps a second encoder engine more available
            # for OBS/recording workloads. H.264 does not expose split-frame mode.
            if encoder in {"hevc_nvenc", "av1_nvenc"} and "-split_encode_mode" in help_text:
                split_disabled = "disabled" if "disabled" in help_text else "15"
                args.extend(["-split_encode_mode", split_disabled])
        return args

    @classmethod
    def _acquire_recording_nvenc_slot(cls, cancel: Event | None) -> bool:
        while True:
            check_cancelled(cancel)
            if cls._nvenc_recording_lock.acquire(timeout=0.2):
                return True

    def probe(self, source: Path) -> dict:
        ffprobe = require_executable("ffprobe")
        result = run_command(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration,size,bit_rate:stream=index,codec_type,codec_name,width,height,r_frame_rate,avg_frame_rate,pix_fmt,color_range,color_space,color_transfer,color_primaries,bits_per_raw_sample",
                "-of",
                "json",
                str(source),
            ],
            timeout=30,
        )
        if result.returncode:
            raise RuntimeError(
                result.stderr.strip() or f"{source.name} konnte nicht analysiert werden."
            )
        return json.loads(result.stdout)

    @staticmethod
    def frame_rate_from_probe(info: dict) -> float:
        stream = next(
            (item for item in info.get("streams", []) if item.get("codec_type") == "video"),
            {},
        )
        for key in ("avg_frame_rate", "r_frame_rate"):
            raw = str(stream.get(key) or "").strip()
            if not raw or raw in {"0/0", "N/A"}:
                continue
            try:
                if "/" in raw:
                    numerator, denominator = raw.split("/", 1)
                    value = float(numerator) / float(denominator)
                else:
                    value = float(raw)
            except (ValueError, ZeroDivisionError):
                continue
            if value > 0:
                return value
        return 0.0

    @staticmethod
    def hdr_info_from_probe(info: dict) -> HdrInfo:
        stream = next(
            (item for item in info.get("streams", []) if item.get("codec_type") == "video"),
            {},
        )
        transfer = str(stream.get("color_transfer") or "").casefold()
        primaries = str(stream.get("color_primaries") or "").casefold()
        colorspace = str(stream.get("color_space") or "").casefold()
        color_range = str(stream.get("color_range") or "").casefold()
        pixel_format = str(stream.get("pix_fmt") or "").casefold()
        return HdrInfo(
            is_hdr=transfer in HDR_TRANSFERS,
            transfer=transfer,
            primaries=primaries,
            colorspace=colorspace,
            color_range=color_range,
            pixel_format=pixel_format,
        )

    def hdr_info(self, source: Path) -> HdrInfo:
        return self.hdr_info_from_probe(self.probe(source))

    def frame_timestamps(self, source: Path) -> list[float]:
        """Return presentation timestamps for every frame of the first video stream."""
        ffprobe = require_executable("ffprobe")
        result = run_command(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "frame=best_effort_timestamp_time",
                "-of",
                "csv=p=0",
                str(source),
            ],
            timeout=180,
        )
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "Frame-Zeitstempel konnten nicht gelesen werden.")
        timestamps: list[float] = []
        for line in result.stdout.splitlines():
            raw = line.strip().split(",", 1)[0]
            if not raw or raw == "N/A":
                continue
            try:
                value = float(raw)
            except ValueError:
                continue
            if value >= 0 and (not timestamps or value > timestamps[-1] + 0.000001):
                timestamps.append(value)
        return timestamps

    @classmethod
    def _require_hdr_tonemap_filters(cls) -> None:
        if cls._hdr_filters_checked:
            return
        ffmpeg = require_executable("ffmpeg")
        result = run_command([ffmpeg, "-hide_banner", "-filters"], timeout=20)
        listing = f"{result.stdout}\n{result.stderr}"
        if result.returncode or " zscale " not in listing or " tonemap " not in listing:
            raise RuntimeError(
                "Für HDR → SDR fehlen in dieser FFmpeg-Version die Filter zscale und/oder tonemap. "
                "Bitte eine vollständige FFmpeg-Build mit libzimg verwenden."
            )
        cls._hdr_filters_checked = True

    @staticmethod
    def _fraction_to_scaled_int(value: object, scale: int) -> int | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            if "/" in raw:
                numerator, denominator = raw.split("/", 1)
                number = float(numerator) / float(denominator)
            else:
                number = float(raw)
        except (ValueError, ZeroDivisionError):
            return None
        return int(round(number * scale))

    def _x265_hdr_side_data_params(self, source: Path) -> list[str]:
        """Extract static HDR10 mastering metadata when present on the first frame."""
        ffprobe = require_executable("ffprobe")
        result = run_command(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_frames",
                "-read_intervals",
                "%+#1",
                "-show_entries",
                "frame=side_data_list",
                "-of",
                "json",
                str(source),
            ],
            timeout=30,
        )
        if result.returncode:
            return []
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return []
        frames = data.get("frames") or []
        side_data = frames[0].get("side_data_list", []) if frames else []
        params: list[str] = []
        for item in side_data:
            kind = str(item.get("side_data_type") or "")
            if kind == "Mastering display metadata":
                values = {
                    "rx": self._fraction_to_scaled_int(item.get("red_x"), 50000),
                    "ry": self._fraction_to_scaled_int(item.get("red_y"), 50000),
                    "gx": self._fraction_to_scaled_int(item.get("green_x"), 50000),
                    "gy": self._fraction_to_scaled_int(item.get("green_y"), 50000),
                    "bx": self._fraction_to_scaled_int(item.get("blue_x"), 50000),
                    "by": self._fraction_to_scaled_int(item.get("blue_y"), 50000),
                    "wx": self._fraction_to_scaled_int(item.get("white_point_x"), 50000),
                    "wy": self._fraction_to_scaled_int(item.get("white_point_y"), 50000),
                    "min_l": self._fraction_to_scaled_int(item.get("min_luminance"), 10000),
                    "max_l": self._fraction_to_scaled_int(item.get("max_luminance"), 10000),
                }
                if all(value is not None for value in values.values()):
                    params.append(
                        "master-display="
                        f"G({values['gx']},{values['gy']})"
                        f"B({values['bx']},{values['by']})"
                        f"R({values['rx']},{values['ry']})"
                        f"WP({values['wx']},{values['wy']})"
                        f"L({values['max_l']},{values['min_l']})"
                    )
            elif kind == "Content light level metadata":
                try:
                    max_content = int(item.get("max_content"))
                    max_average = int(item.get("max_average"))
                except (TypeError, ValueError):
                    continue
                params.append(f"max-cll={max_content},{max_average}")
        return params

    @staticmethod
    def _safe_stem(value: str, fallback: str) -> str:
        raw = value.strip().replace("\\", "/").rsplit("/", 1)[-1] if value.strip() else fallback
        known_suffixes = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".ts", ".mts"}
        suffix = Path(raw).suffix.casefold()
        stem = raw[: -len(suffix)] if suffix in known_suffixes else raw
        stem = stem.strip(" .") or fallback
        invalid = '<>:"/\\|?*'
        stem = "".join("_" if char in invalid or ord(char) < 32 else char for char in stem)
        stem = stem.strip(" .") or fallback
        reserved = {
            "CON", "PRN", "AUX", "NUL",
            *(f"COM{number}" for number in range(1, 10)),
            *(f"LPT{number}" for number in range(1, 10)),
        }
        if stem.upper() in reserved:
            stem += "_"
        return stem

    @staticmethod
    def _segment_bounds(
        full_duration: float, start_seconds: float | None, end_seconds: float | None
    ) -> tuple[float, float, float]:
        start = max(0.0, float(start_seconds or 0.0))
        end = float(end_seconds) if end_seconds is not None else full_duration
        if full_duration:
            end = min(end, full_duration)
        if end <= start:
            raise ValueError("Das Segmentende muss nach dem Start liegen.")
        if full_duration and start >= full_duration:
            raise ValueError("Der Segmentstart liegt hinter dem Videoende.")
        return start, end, end - start

    def compress_one(
        self,
        source: Path,
        output_dir: Path,
        options: VideoOptions,
        progress: ProgressCallback = noop_progress,
        cancel: Event | None = None,
        start_seconds: float | None = None,
        end_seconds: float | None = None,
        output_name: str | None = None,
        color_mode: str = "source",
        tone_map: str = "hable",
    ) -> Path:
        if not source.is_file():
            raise FileNotFoundError(source)
        check_cancelled(cancel)
        info = self.probe(source)
        full_duration = float(info.get("format", {}).get("duration") or 0)
        if start_seconds is not None or end_seconds is not None:
            start, _end, duration = self._segment_bounds(
                full_duration, start_seconds, end_seconds
            )
        else:
            start, duration = 0.0, full_duration or None
        container = options.container.lower()
        if container not in {"mp4", "mkv", "webm"}:
            raise ValueError("Container muss MP4, MKV oder WebM sein.")
        codec, is_nvenc = self._resolve_encoder(options)
        if container == "webm" and str(options.codec).casefold() in {"h264", "h265"}:
            raise ValueError("WebM benötigt VP9 oder AV1.")
        if output_name:
            stem = self._safe_stem(output_name, f"{source.stem}_segment")
            output = unique_output(output_dir / f"{stem}.{container}")
        else:
            output = unique_output(output_dir / f"{source.stem}_komprimiert.{container}")
        handle, temp_name = tempfile.mkstemp(
            prefix=f".{output.stem}-", suffix=output.suffix, dir=output.parent
        )
        os.close(handle)
        temp = Path(temp_name)
        filters: list[str] = []
        video_stream = next(
            (stream for stream in info.get("streams", []) if stream.get("codec_type") == "video"),
            {},
        )
        source_height = int(video_stream.get("height") or 0)
        hdr_info = self.hdr_info_from_probe(info)
        color_mode = str(color_mode or "source").casefold()
        if color_mode not in {"source", "hdr", "sdr"}:
            raise ValueError(f"Unbekannter Farbmodus: {color_mode}")
        if color_mode in {"hdr", "sdr"} and not hdr_info.is_hdr:
            raise ValueError(
                "Die Quelle ist nicht als HDR10/PQ oder HLG gekennzeichnet. "
                "HDR+SDR-Dual-Export wurde deshalb nicht gestartet."
            )
        tone_map = str(tone_map or "hable").casefold()
        if tone_map not in {"hable", "mobius", "reinhard"}:
            tone_map = "hable"
        if color_mode == "sdr":
            self._require_hdr_tonemap_filters()
            # Tone mapping must happen in linear light. zscale converts the tagged
            # HDR source to linear RGB; tonemap compresses the dynamic range and
            # the final zscale produces standard BT.709 limited-range SDR.
            filters.extend(
                [
                    "zscale=t=linear",
                    "format=gbrpf32le",
                    "zscale=p=bt709",
                    f"tonemap=tonemap={tone_map}",
                    "zscale=t=bt709:m=bt709:r=tv",
                    "format=yuv420p",
                ]
            )
        if options.height and (not source_height or source_height > int(options.height)):
            # "Maximale Auflösung" means cap, not upscale. SD/1080p sources stay native.
            filters.append(f"scale=-2:{int(options.height)}")
        if options.rotation == 90:
            filters.append("transpose=1")
        elif options.rotation == 180:
            filters.extend(["hflip", "vflip"])
        elif options.rotation == 270:
            filters.append("transpose=2")
        args = ["-y"]
        if start_seconds is not None or end_seconds is not None:
            args.extend(["-ss", f"{start:.3f}", "-t", f"{float(duration):.3f}"])
        args.extend(["-i", str(source), "-map", "0:v:0", "-map", "0:a?", "-c:v", codec])
        if filters:
            args.extend(["-vf", ",".join(filters)])
        if options.fps:
            args.extend(["-r", str(int(options.fps))])
        preset = self.PRESETS.get(options.preset, self.PRESETS["balanced"])
        if is_nvenc:
            args.extend(self._nvenc_tuning_args(codec, options))
        elif codec in {"libx264", "libx265"}:
            args.extend(["-preset", preset.get(codec, "medium")])
        elif codec == "libsvtav1":
            args.extend(["-preset", preset.get(codec, "8")])
        if color_mode == "hdr":
            if codec not in {"libx265", "libsvtav1", "hevc_nvenc", "av1_nvenc"}:
                raise ValueError(
                    "Für die HDR-Ausgabe wird H.265/HEVC oder AV1 benötigt. "
                    "Bitte einen HDR-Codec auswählen."
                )
            transfer = hdr_info.transfer or "smpte2084"
            primaries = hdr_info.primaries or "bt2020"
            colorspace = hdr_info.colorspace or "bt2020nc"
            color_range = hdr_info.color_range or "tv"
            args.extend(
                [
                    "-pix_fmt",
                    "p010le" if is_nvenc else "yuv420p10le",
                    "-color_primaries",
                    primaries,
                    "-color_trc",
                    transfer,
                    "-colorspace",
                    colorspace,
                    "-color_range",
                    color_range,
                ]
            )
            if codec == "libx265":
                x265_params = [
                    "repeat-headers=1",
                    f"colorprim={primaries}",
                    f"transfer={transfer}",
                    f"colormatrix={colorspace}",
                ]
                if transfer == "smpte2084":
                    x265_params.append("hdr-opt=1")
                    x265_params.extend(self._x265_hdr_side_data_params(source))
                args.extend(["-x265-params", ":".join(x265_params)])
            if container == "mp4" and codec in {"libx265", "hevc_nvenc"}:
                args.extend(["-tag:v", "hvc1"])
        elif color_mode == "sdr":
            args.extend(
                [
                    "-pix_fmt",
                    "yuv420p",
                    "-color_primaries",
                    "bt709",
                    "-color_trc",
                    "bt709",
                    "-colorspace",
                    "bt709",
                    "-color_range",
                    "tv",
                ]
            )
        target_encoding = bool(options.target_mb and duration)
        if target_encoding:
            # Reserve a little container overhead so uploads stay below the selected cap.
            total_kbps = options.target_mb * 1024 * 1024 * 8 / float(duration) / 1000 * 0.965
            audio_kbps = 0 if options.mute else options.audio_bitrate
            video_kbps = int(total_kbps - audio_kbps - 24)
            if video_kbps < 100:
                raise ValueError(
                    "Die Zielgröße ist für Dauer und Audiospur zu klein. Bitte Größe erhöhen, Ton entfernen oder Segment kürzen."
                )
            if is_nvenc:
                args.extend(["-rc", "vbr"])
            args.extend(
                [
                    "-b:v",
                    f"{video_kbps}k",
                    "-maxrate",
                    f"{int(video_kbps * 1.08)}k",
                    "-bufsize",
                    f"{video_kbps * 2}k",
                ]
            )
        elif is_nvenc:
            # NVENC's VBR-CQ is the hardware equivalent of the profile's CRF quality intent.
            args.extend(["-rc", "vbr", "-cq", str(options.crf), "-b:v", "0"])
        elif codec in {"libvpx-vp9", "libsvtav1"}:
            args.extend(["-b:v", "0", "-crf", str(options.crf)])
        else:
            args.extend(["-crf", str(options.crf)])
        if options.mute:
            args.append("-an")
        else:
            audio_codec = "libopus" if container == "webm" else "aac"
            args.extend(["-c:a", audio_codec, "-b:a", f"{options.audio_bitrate}k"])
        if container == "mp4":
            args.extend(["-movflags", "+faststart"])
        args.append(str(temp))
        recording_safe = is_nvenc and str(options.nvenc_mode or "quality").casefold() == "recording"
        recording_slot = False
        try:
            if recording_safe:
                progress(0, "NVENC-Aufnahmemodus: warte auf freien Hintergrund-Encoder")
                recording_slot = self._acquire_recording_nvenc_slot(cancel)
            if target_encoding and codec in {"libx264", "libx265"}:
                with tempfile.TemporaryDirectory(prefix="aio-video-pass-") as pass_directory:
                    passlog = str(Path(pass_directory) / "ffmpeg2pass")
                    first_args = args[:-1]
                    # Remove optional audio mapping/encoding for a faster analysis pass.
                    for index in range(len(first_args) - 1, 0, -1):
                        if first_args[index - 1 : index + 1] == ["-map", "0:a?"]:
                            del first_args[index - 1 : index + 1]
                    first_args.extend(
                        ["-an", "-pass", "1", "-passlogfile", passlog, "-f", "null", os.devnull]
                    )
                    run_ffmpeg(
                        first_args,
                        duration_seconds=duration,
                        progress=lambda value, message: progress(value // 2, message),
                        cancel=cancel,
                        cpu_limit_percent=options.cpu_limit_percent,
                        cpu_mode=options.cpu_mode,
                        background_priority=recording_safe,
                    )
                    second_args = args[:-1]
                    second_args.extend(["-pass", "2", "-passlogfile", passlog, str(temp)])
                    run_ffmpeg(
                        second_args,
                        duration_seconds=duration,
                        progress=lambda value, message: progress(50 + value // 2, message),
                        cancel=cancel,
                        cpu_limit_percent=options.cpu_limit_percent,
                        cpu_mode=options.cpu_mode,
                        background_priority=recording_safe,
                    )
            else:
                run_ffmpeg(
                    args,
                    duration_seconds=duration,
                    progress=progress,
                    cancel=cancel,
                    cpu_limit_percent=options.cpu_limit_percent,
                    cpu_mode=options.cpu_mode,
                    background_priority=recording_safe,
                )
            check_cancelled(cancel)
            if not temp.exists() or temp.stat().st_size == 0:
                raise RuntimeError("FFmpeg hat keine gültige Ausgabedatei erzeugt.")
            os.replace(temp, output)
        finally:
            if recording_slot:
                self._nvenc_recording_lock.release()
            temp.unlink(missing_ok=True)
        progress(100, f"{output.name} fertig")
        return output

    def cut_segment(
        self,
        source: Path,
        output_dir: Path,
        options: CutOptions,
        progress: ProgressCallback = noop_progress,
        cancel: Event | None = None,
    ) -> Path:
        """Cut a clip without re-encoding. Boundaries follow available keyframes."""
        if not source.is_file():
            raise FileNotFoundError(source)
        check_cancelled(cancel)
        info = self.probe(source)
        full_duration = float(info.get("format", {}).get("duration") or 0)
        start, _end, duration = self._segment_bounds(
            full_duration, options.start_seconds, options.end_seconds
        )
        suffix = source.suffix.casefold() or ".mkv"
        stem = self._safe_stem(options.output_name, f"{source.stem}_segment")
        output = unique_output(output_dir / f"{stem}{suffix}")
        handle, temp_name = tempfile.mkstemp(
            prefix=f".{output.stem}-", suffix=output.suffix, dir=output.parent
        )
        os.close(handle)
        temp = Path(temp_name)
        try:
            # Pure stream-copy cannot guarantee an exact non-zero start: FFmpeg may have
            # to include the previous keyframe (in long-GOP material this can reach far
            # back into the source). Keep the zero-start fast path, but re-encode video
            # at very high quality for non-zero starts so the requested range is a hard
            # boundary. Audio/subtitles are still copied when possible.
            if start <= 0.0005:
                args = [
                    "-y",
                    "-t",
                    f"{duration:.3f}",
                    "-i",
                    str(source),
                    "-map",
                    "0:v?",
                    "-map",
                    "0:a?",
                    "-map",
                    "0:s?",
                    "-c",
                    "copy",
                    "-avoid_negative_ts",
                    "make_zero",
                    str(temp),
                ]
            else:
                video_stream = next(
                    (stream for stream in info.get("streams", []) if stream.get("codec_type") == "video"),
                    {},
                )
                codec_name = str(video_stream.get("codec_name") or "").casefold()
                if codec_name == "h264":
                    video_args = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "16"]
                elif codec_name in {"hevc", "h265"}:
                    video_args = ["-c:v", "libx265", "-preset", "veryfast", "-crf", "16"]
                elif codec_name == "vp9":
                    video_args = ["-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "18", "-row-mt", "1"]
                elif codec_name == "av1":
                    video_args = ["-c:v", "libsvtav1", "-preset", "8", "-crf", "18"]
                elif codec_name == "mpeg4":
                    video_args = ["-c:v", "mpeg4", "-q:v", "2"]
                else:
                    video_args = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "16"]
                # Output-side seeking is intentional here. The video is decoded and
                # re-encoded from the exact requested timestamp, while copied audio is
                # also dropped before that point instead of keeping input-seek preroll.
                args = [
                    "-y",
                    "-i",
                    str(source),
                    "-ss",
                    f"{start:.6f}",
                    "-t",
                    f"{duration:.6f}",
                    "-map",
                    "0:v?",
                    "-map",
                    "0:a?",
                    "-map",
                    "0:s?",
                    *video_args,
                    "-c:a",
                    "copy",
                    "-c:s",
                    "copy",
                    "-avoid_negative_ts",
                    "make_zero",
                    str(temp),
                ]
            run_ffmpeg(
                args,
                duration_seconds=duration,
                progress=progress,
                cancel=cancel,
                cpu_limit_percent=options.cpu_limit_percent,
                cpu_mode=options.cpu_mode,
            )
            check_cancelled(cancel)
            if not temp.exists() or temp.stat().st_size == 0:
                raise RuntimeError("FFmpeg hat kein gültiges Video-Segment erzeugt.")
            os.replace(temp, output)
        finally:
            temp.unlink(missing_ok=True)
        progress(100, f"{output.name} fertig")
        return output

    def compress_hdr_sdr_pair(
        self,
        source: Path,
        output_dir: Path,
        options: VideoOptions,
        hdr_codec: str = "h265",
        tone_map: str = "hable",
        progress: ProgressCallback = noop_progress,
        cancel: Event | None = None,
    ) -> list[Path]:
        """Create a compressed HDR master plus an SDR tone-mapped companion file."""
        if not source.is_file():
            raise FileNotFoundError(source)
        info = self.probe(source)
        hdr = self.hdr_info_from_probe(info)
        if not hdr.is_hdr:
            raise ValueError(
                f"{source.name} ist nicht als HDR10/PQ oder HLG gekennzeichnet. "
                "Dual-Export ist nur für erkannte HDR-Quellen verfügbar."
            )
        self._require_hdr_tonemap_filters()
        hdr_codec = str(hdr_codec or "h265").casefold()
        if hdr_codec not in {"h265", "av1"}:
            raise ValueError("HDR-Codec muss H.265/HEVC oder AV1 sein.")

        hdr_container = options.container
        if hdr_container == "webm" and hdr_codec == "h265":
            hdr_container = "mkv"
        hdr_options = replace(options, codec=hdr_codec, container=hdr_container)
        sdr_options = replace(options)

        hdr_output = self.compress_one(
            source,
            output_dir,
            hdr_options,
            progress=lambda value, message: progress(value // 2, f"HDR: {message}"),
            cancel=cancel,
            output_name=f"{source.stem}_HDR",
            color_mode="hdr",
        )
        check_cancelled(cancel)
        sdr_output = self.compress_one(
            source,
            output_dir,
            sdr_options,
            progress=lambda value, message: progress(50 + value // 2, f"SDR: {message}"),
            cancel=cancel,
            output_name=f"{source.stem}_SDR",
            color_mode="sdr",
            tone_map=tone_map,
        )
        progress(100, f"{source.name}: HDR + SDR fertig")
        return [hdr_output, sdr_output]

    def compress_many_hdr_sdr(
        self,
        sources: list[Path],
        output_dir: Path,
        options: VideoOptions,
        hdr_codec: str = "h265",
        tone_map: str = "hable",
        progress: ProgressCallback = noop_progress,
        cancel: Event | None = None,
    ) -> list[Path]:
        if not sources:
            raise ValueError("Bitte mindestens ein Video auswählen.")
        invalid_sources = [source.name for source in sources if not self.hdr_info(source).is_hdr]
        if invalid_sources:
            names = ", ".join(invalid_sources[:5])
            if len(invalid_sources) > 5:
                names += f" (+{len(invalid_sources) - 5} weitere)"
            raise ValueError(
                "HDR+SDR-Dual-Export wurde nicht gestartet, weil folgende Dateien nicht als "
                f"HDR10/PQ oder HLG erkannt wurden: {names}"
            )
        self._require_hdr_tonemap_filters()
        results: list[Path] = []
        for index, source in enumerate(sources):
            check_cancelled(cancel)

            def item_progress(
                value: int,
                message: str,
                item_index: int = index,
                source_count: int = len(sources),
            ) -> None:
                progress(int((item_index + value / 100) / source_count * 100), message)

            results.extend(
                self.compress_hdr_sdr_pair(
                    source,
                    output_dir,
                    options,
                    hdr_codec=hdr_codec,
                    tone_map=tone_map,
                    progress=item_progress,
                    cancel=cancel,
                )
            )
        return results

    def compress_many(
        self,
        sources: list[Path],
        output_dir: Path,
        options: VideoOptions,
        progress: ProgressCallback = noop_progress,
        cancel: Event | None = None,
    ) -> list[Path]:
        if not sources:
            raise ValueError("Bitte mindestens ein Video auswählen.")
        results: list[Path] = []
        for index, source in enumerate(sources):
            check_cancelled(cancel)

            def item_progress(
                value: int,
                message: str,
                item_index: int = index,
                source_count: int = len(sources),
            ) -> None:
                progress(int((item_index + value / 100) / source_count * 100), message)

            results.append(self.compress_one(source, output_dir, options, item_progress, cancel))
        return results

    def segment_to_gif(
        self,
        source: Path,
        output_dir: Path,
        options: GifOptions,
        progress: ProgressCallback = noop_progress,
        cancel: Event | None = None,
    ) -> Path:
        if not source.is_file():
            raise FileNotFoundError(source)
        info = self.probe(source)
        full_duration = float(info.get("format", {}).get("duration") or 0)
        start = max(0.0, float(options.start_seconds))
        end = float(options.end_seconds)
        if end <= start:
            raise ValueError("Das Segmentende muss nach dem Start liegen.")
        if full_duration and start >= full_duration:
            raise ValueError("Der Segmentstart liegt hinter dem Videoende.")
        end = min(end, full_duration) if full_duration else end
        duration = end - start
        if duration > 120:
            raise ValueError("Ein GIF-Segment darf höchstens 120 Sekunden lang sein.")
        fps = max(4, min(30, int(options.fps)))
        width = max(160, min(3840, int(options.width)))
        colors = max(32, min(256, int(options.colors)))
        output = unique_output(output_dir / f"{source.stem}_{start:g}-{end:g}s.gif")
        handle, temp_name = tempfile.mkstemp(
            prefix=f".{output.stem}-", suffix=".gif", dir=output.parent
        )
        os.close(handle)
        temp = Path(temp_name)
        graph = (
            f"fps={fps},scale={width}:-1:flags=lanczos,split[a][b];"
            f"[a]palettegen=max_colors={colors}:stats_mode=diff[p];"
            "[b][p]paletteuse=dither=bayer:bayer_scale=3:diff_mode=rectangle"
        )
        try:
            run_ffmpeg(
                [
                    "-y",
                    "-ss",
                    f"{start:.3f}",
                    "-t",
                    f"{duration:.3f}",
                    "-i",
                    str(source),
                    "-filter_complex",
                    graph,
                    "-loop",
                    str(max(0, int(options.loop))),
                    str(temp),
                ],
                duration_seconds=duration,
                progress=progress,
                cancel=cancel,
                cpu_limit_percent=options.cpu_limit_percent,
                cpu_mode=options.cpu_mode,
            )
            check_cancelled(cancel)
            if not temp.exists() or temp.stat().st_size == 0:
                raise RuntimeError("FFmpeg hat kein gültiges GIF erzeugt.")
            os.replace(temp, output)
        finally:
            temp.unlink(missing_ok=True)
        progress(100, f"{output.name} fertig")
        return output
