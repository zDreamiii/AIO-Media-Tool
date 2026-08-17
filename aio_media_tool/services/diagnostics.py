from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys
import zipfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path

from aio_media_tool import __version__
from aio_media_tool.models import AppSettings
from aio_media_tool.services.common import run_command


@dataclass(frozen=True, slots=True)
class ToolStatus:
    name: str
    available: bool
    version: str
    note: str = ""


def _command_version(name: str, args: list[str]) -> ToolStatus:
    executable = shutil.which(name)
    if not executable:
        return ToolStatus(name, False, "—", "Nicht im PATH gefunden")
    try:
        result = run_command([executable, *args], timeout=8)
        line = (result.stdout or result.stderr).strip().splitlines()[0]
        return ToolStatus(name, result.returncode == 0, line[:180])
    except (OSError, subprocess.SubprocessError) as exc:
        return ToolStatus(name, False, "—", type(exc).__name__)


def collect_tool_status() -> list[ToolStatus]:
    statuses = [
        ToolStatus("Python", True, platform.python_version()),
        _command_version("ffmpeg", ["-version"]),
        _command_version("ffprobe", ["-version"]),
        _command_version("git", ["--version"]),
        _command_version("uv", ["--version"]),
        _command_version("tesseract", ["--version"]),
        _command_version("exiftool", ["-ver"]),
        _command_version("realesrgan-ncnn-vulkan", ["-h"]),
        _command_version("rife-ncnn-vulkan", ["-h"]),
    ]
    for package in (
        "PySide6",
        "Pillow",
        "pypdf",
        "mutagen",
        "yt-dlp",
        "cryptography",
        "python-docx",
        "faster-whisper",
        "PyMuPDF",
    ):
        try:
            statuses.append(ToolStatus(package, True, metadata.version(package)))
        except metadata.PackageNotFoundError:
            statuses.append(ToolStatus(package, False, "—", "Python-Paket fehlt"))
    return statuses


def create_diagnostic_bundle(output_dir: Path, settings: AppSettings, log_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output = output_dir / f"diagnose-{stamp}.zip"
    safe_settings = {
        "theme": settings.theme,
        "language": settings.language,
        "parallel_jobs": settings.parallel_jobs,
        "update_mode": settings.update_mode,
        "update_remote": settings.update_remote,
        "detailed_logs": settings.detailed_logs,
    }
    report = {
        "created_at": datetime.now(UTC).isoformat(),
        "app_version": __version__,
        "platform": platform.platform(),
        "python": sys.version,
        "settings": safe_settings,
        "tools": [asdict(status) for status in collect_tool_status()],
    }
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("diagnostics.json", json.dumps(report, indent=2, ensure_ascii=False))
        if settings.detailed_logs and log_dir.exists():
            for log in sorted(log_dir.glob("*.log"))[-3:]:
                archive.write(log, f"logs/{log.name}")
    return output
