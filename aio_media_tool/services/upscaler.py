from __future__ import annotations

import os
import platform
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from fractions import Fraction
from pathlib import Path
from threading import Event

from aio_media_tool.models import JobCancelled
from aio_media_tool.services.common import (
    ProgressCallback,
    check_cancelled,
    noop_progress,
    require_executable,
    run_command,
    run_ffmpeg,
    unique_output,
)
from aio_media_tool.services.video import VideoService

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v"}


@dataclass(frozen=True, slots=True)
class HardwareReport:
    cuda: str
    vulkan: str
    directml: str
    recommendation: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(slots=True)
class UpscaleOptions:
    scale: int = 2
    model: str = "realesrgan-x4plus"
    interpolate: bool = False
    target_fps: int = 60
    tile_size: int = 0
    gpu_id: int = 0
    uhd: bool = False


class UpscalerService:
    @staticmethod
    def detect_hardware() -> HardwareReport:
        cuda = "Nicht erkannt"
        nvidia = shutil.which("nvidia-smi")
        if nvidia:
            result = run_command(
                [nvidia, "--query-gpu=name,memory.total", "--format=csv,noheader"], timeout=8
            )
            if result.returncode == 0 and result.stdout.strip():
                cuda = result.stdout.strip().replace("\n", "; ")[:400]
        vulkan = "Nicht erkannt"
        vulkaninfo = shutil.which("vulkaninfo")
        if vulkaninfo:
            result = run_command([vulkaninfo, "--summary"], timeout=10)
            if result.returncode == 0:
                lines = [
                    line.strip() for line in result.stdout.splitlines() if "deviceName" in line
                ]
                vulkan = "; ".join(lines)[:400] or "Vulkan-Laufzeit verfügbar"
        elif sys_platform_is_windows() and os.environ.get("VK_SDK_PATH"):
            vulkan = "Vulkan SDK erkannt"
        directml = "Nicht erkannt"
        if sys_platform_is_windows():
            try:
                import torch_directml

                directml = str(torch_directml.device())
            except (ImportError, OSError, RuntimeError):
                directml = "Windows vorhanden, Python-Backend fehlt"
        recommendation = (
            "CUDA/faster-whisper und Vulkan/NCNN"
            if cuda != "Nicht erkannt" and vulkan != "Nicht erkannt"
            else "Vulkan/NCNN"
            if vulkan != "Nicht erkannt"
            else "CPU/Fallback oder GPU-Treiber prüfen"
        )
        return HardwareReport(cuda, vulkan, directml, recommendation)

    @staticmethod
    def resolve_binary(value: str, default_name: str) -> str:
        if value.strip():
            candidate = Path(value).expanduser()
            if candidate.is_file():
                return str(candidate.resolve())
            raise FileNotFoundError(f"Binary nicht gefunden: {candidate}")
        detected = shutil.which(default_name)
        if not detected:
            raise RuntimeError(
                f"{default_name} wurde nicht gefunden. Portable Binary auswählen oder zum PATH hinzufügen."
            )
        return detected

    def process(
        self,
        source: Path,
        output_dir: Path,
        private_temp: Path,
        options: UpscaleOptions,
        realesrgan_path: str = "",
        rife_path: str = "",
        progress: ProgressCallback = noop_progress,
        cancel: Event | None = None,
    ) -> list[Path]:
        suffix = source.suffix.casefold()
        if suffix in IMAGE_EXTENSIONS:
            binary = self.resolve_binary(realesrgan_path, "realesrgan-ncnn-vulkan")
            return [self.upscale_image(source, output_dir, options, binary, progress, cancel)]
        if suffix in VIDEO_EXTENSIONS:
            return [
                self.process_video(
                    source,
                    output_dir,
                    private_temp,
                    options,
                    realesrgan_path,
                    rife_path,
                    progress,
                    cancel,
                )
            ]
        raise ValueError("Upscaling unterstützt JPG, PNG, WebP und gängige Videoformate.")

    def upscale_image(
        self,
        source: Path,
        output_dir: Path,
        options: UpscaleOptions,
        binary: str,
        progress: ProgressCallback = noop_progress,
        cancel: Event | None = None,
    ) -> Path:
        source = source.expanduser().resolve()
        output_dir = output_dir.expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        scale = int(options.scale)
        if scale not in {2, 3, 4}:
            raise ValueError("Real-ESRGAN unterstützt die Skalierungen 2×, 3× und 4×.")
        output_dir.mkdir(parents=True, exist_ok=True)
        output = unique_output(output_dir / f"{source.stem}_{scale}x.png")
        temp = output.with_name(f".{output.stem}-{os.urandom(5).hex()}.png")
        command = [
            binary,
            "-i",
            str(source),
            "-o",
            str(temp),
            "-n",
            options.model,
            "-s",
            str(scale),
            "-t",
            str(max(0, int(options.tile_size))),
            "-f",
            "png",
        ]
        try:
            self._run_tool(command, temp.parent, 1, 0, 99, progress, cancel)
            if not temp.is_file() or not temp.stat().st_size:
                raise RuntimeError("Real-ESRGAN hat kein Bild erzeugt.")
            os.replace(temp, output)
        finally:
            temp.unlink(missing_ok=True)
        progress(100, f"Upscaling fertig: {output.name}")
        return output

    def process_video(
        self,
        source: Path,
        output_dir: Path,
        private_temp: Path,
        options: UpscaleOptions,
        realesrgan_path: str,
        rife_path: str,
        progress: ProgressCallback = noop_progress,
        cancel: Event | None = None,
    ) -> Path:
        source = source.expanduser().resolve()
        output_dir = output_dir.expanduser().resolve()
        private_temp = private_temp.expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        if options.scale not in {1, 2, 3, 4} or (options.scale == 1 and not options.interpolate):
            raise ValueError("Bitte Upscaling oder Interpolation konfigurieren.")
        realesrgan = (
            self.resolve_binary(realesrgan_path, "realesrgan-ncnn-vulkan")
            if options.scale > 1
            else ""
        )
        rife = self.resolve_binary(rife_path, "rife-ncnn-vulkan") if options.interpolate else ""
        require_executable("ffmpeg")
        info = VideoService().probe(source)
        duration = float(info.get("format", {}).get("duration") or 0)
        video_stream = next(
            (stream for stream in info.get("streams", []) if stream.get("codec_type") == "video"),
            None,
        )
        if not video_stream:
            raise ValueError("Die Datei enthält keine Videospur.")
        try:
            source_fps = float(Fraction(str(video_stream.get("r_frame_rate") or "0/1")))
        except (ValueError, ZeroDivisionError):
            source_fps = 0
        source_fps = source_fps or 30.0
        private_temp.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="aio-upscale-", dir=private_temp) as directory:
            work = Path(directory)
            input_frames = work / "input_frames"
            input_frames.mkdir()
            run_ffmpeg(
                ["-y", "-i", str(source), str(input_frames / "%08d.png")],
                duration_seconds=duration or None,
                progress=lambda value, message: progress(value // 10, message),
                cancel=cancel,
            )
            frame_count = len(list(input_frames.glob("*.png")))
            if not frame_count:
                raise RuntimeError("FFmpeg konnte keine Videoframes extrahieren.")
            active_frames = input_frames
            output_fps = source_fps
            stage_start = 10
            if options.interpolate:
                interpolated = work / "interpolated_frames"
                interpolated.mkdir()
                target_fps = max(int(round(source_fps)), int(options.target_fps))
                target_count = max(
                    frame_count + 1, int(round(frame_count * target_fps / source_fps))
                )
                command = [
                    rife,
                    "-i",
                    str(active_frames),
                    "-o",
                    str(interpolated),
                    "-n",
                    str(target_count),
                ]
                if options.uhd:
                    command.append("-u")
                interpolation_end = 48 if options.scale > 1 else 84
                self._run_tool(
                    command, interpolated, target_count, 10, interpolation_end, progress, cancel
                )
                active_frames = interpolated
                frame_count = len(list(active_frames.glob("*.png")))
                if not frame_count:
                    raise RuntimeError("RIFE hat keine interpolierten Frames erzeugt.")
                output_fps = target_fps
                stage_start = interpolation_end
            processed_frames = active_frames
            if options.scale > 1:
                upscaled = work / "upscaled_frames"
                upscaled.mkdir()
                command = [
                    realesrgan,
                    "-i",
                    str(active_frames),
                    "-o",
                    str(upscaled),
                    "-n",
                    options.model,
                    "-s",
                    str(int(options.scale)),
                    "-t",
                    str(max(0, int(options.tile_size))),
                    "-f",
                    "png",
                ]
                self._run_tool(command, upscaled, frame_count, stage_start, 84, progress, cancel)
                if not any(upscaled.glob("*.png")):
                    raise RuntimeError("Real-ESRGAN hat keine hochskalierten Frames erzeugt.")
                processed_frames = upscaled
            output = unique_output(
                output_dir / f"{source.stem}_{options.scale}x_{round(output_fps)}fps.mp4"
            )
            temp = output.with_name(f".{output.stem}-{os.urandom(5).hex()}.mp4")
            try:
                run_ffmpeg(
                    [
                        "-y",
                        "-framerate",
                        f"{output_fps:.6f}",
                        "-i",
                        str(processed_frames / "%08d.png"),
                        "-i",
                        str(source),
                        "-map",
                        "0:v:0",
                        "-map",
                        "1:a?",
                        "-c:v",
                        "libx264",
                        "-preset",
                        "slow",
                        "-crf",
                        "18",
                        "-pix_fmt",
                        "yuv420p",
                        "-c:a",
                        "copy",
                        "-shortest",
                        "-movflags",
                        "+faststart",
                        str(temp),
                    ],
                    duration_seconds=duration or None,
                    progress=lambda value, message: progress(84 + value * 15 // 100, message),
                    cancel=cancel,
                )
                if not temp.is_file() or not temp.stat().st_size:
                    raise RuntimeError("FFmpeg hat kein Upscale-Video erzeugt.")
                os.replace(temp, output)
            finally:
                temp.unlink(missing_ok=True)
        progress(100, f"KI-Medienjob fertig: {output.name}")
        return output

    @staticmethod
    def _run_tool(
        command: list[str],
        output_dir: Path,
        expected_count: int,
        start_percent: int,
        end_percent: int,
        progress: ProgressCallback,
        cancel: Event | None,
    ) -> None:
        handle, log_name = tempfile.mkstemp(prefix=".aio-tool-", suffix=".log", dir=output_dir)
        os.close(handle)
        log_path = Path(log_name)
        existing = {
            path.name
            for path in output_dir.iterdir()
            if path.suffix.casefold() in {".png", ".jpg", ".jpeg", ".webp"}
        }
        started = time.monotonic()
        try:
            with log_path.open("w+", encoding="utf-8") as log:
                process = subprocess.Popen(
                    command,
                    cwd=Path(command[0]).resolve().parent,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    shell=False,
                )
                try:
                    while process.poll() is None:
                        check_cancelled(cancel)
                        produced = len(
                            [
                                path
                                for path in output_dir.iterdir()
                                if path.name not in existing
                                and path.suffix.casefold() in {".png", ".jpg", ".jpeg", ".webp"}
                            ]
                        )
                        fraction = min(0.98, produced / max(1, expected_count))
                        elapsed = max(0.1, time.monotonic() - started)
                        eta = int(elapsed / fraction - elapsed) if fraction > 0.01 else 0
                        value = start_percent + int((end_percent - start_percent) * fraction)
                        message = f"KI-Engine: {produced}/{expected_count} Dateien"
                        if eta:
                            message += f" · ca. {eta // 60}:{eta % 60:02d} verbleibend"
                        progress(value, message)
                        time.sleep(0.25)
                except JobCancelled:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    raise
                if process.returncode:
                    log.seek(0)
                    detail = log.read()[-1800:]
                    raise RuntimeError(
                        detail.strip() or f"KI-Engine endete mit Code {process.returncode}."
                    )
        finally:
            log_path.unlink(missing_ok=True)

    def create_preview(
        self,
        source: Path,
        preview_root: Path,
        private_temp: Path,
        options: UpscaleOptions,
        realesrgan_path: str = "",
        rife_path: str = "",
        progress: ProgressCallback = noop_progress,
        cancel: Event | None = None,
    ) -> tuple[Path, Path]:
        preview_root.mkdir(parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix="preview-", dir=preview_root))
        if source.suffix.casefold() in IMAGE_EXTENSIONS:
            processed = self.process(
                source,
                directory,
                private_temp,
                options,
                realesrgan_path,
                rife_path,
                progress,
                cancel,
            )[0]
            return source, processed
        clip = directory / "sample-1s.mp4"
        run_ffmpeg(
            [
                "-y",
                "-ss",
                "0",
                "-i",
                str(source),
                "-t",
                "1",
                "-c:v",
                "libx264",
                "-crf",
                "18",
                "-an",
                str(clip),
            ],
            duration_seconds=1,
            progress=lambda value, message: progress(value // 10, message),
            cancel=cancel,
        )
        processed_clip = self.process_video(
            clip,
            directory,
            private_temp,
            options,
            realesrgan_path,
            rife_path,
            lambda value, message: progress(10 + value * 80 // 100, message),
            cancel,
        )
        before, after = directory / "before.png", directory / "after.png"
        for video, target in ((clip, before), (processed_clip, after)):
            run_ffmpeg(
                ["-y", "-ss", "0.5", "-i", str(video), "-frames:v", "1", str(target)],
                duration_seconds=1,
                cancel=cancel,
            )
        progress(100, "1-Sekunden-Vorschau gerendert")
        return before, after


def sys_platform_is_windows() -> bool:
    return platform.system().casefold() == "windows"
