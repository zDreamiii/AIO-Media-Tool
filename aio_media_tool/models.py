from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4


class JobKind(StrEnum):
    DOWNLOAD = "download"
    MUSIC = "music"
    IMAGE = "image"
    VIDEO = "video"
    PDF = "pdf"
    AUDIO = "audio"
    RENAME = "rename"
    BOARD = "board"
    TRANSCRIPTION = "transcription"
    PRIVACY = "privacy"
    VAULT = "vault"
    OCR = "ocr"
    UPSCALE = "upscale"
    DIAGNOSTIC = "diagnostic"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(slots=True)
class JobRecord:
    kind: JobKind
    label: str
    source: str
    destination: str
    payload: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid4().hex)
    status: JobStatus = JobStatus.QUEUED
    progress: int = 0
    message: str = "Wartet"
    outputs: list[str] = field(default_factory=list)
    error: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def touch(self) -> None:
        self.updated_at = datetime.now(UTC).isoformat()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["kind"] = self.kind.value
        data["status"] = self.status.value
        return data


class JobCancelled(RuntimeError):
    pass


@dataclass(slots=True)
class AppSettings:
    theme: str = "dark"
    language: str = "de"
    parallel_jobs: int = 2
    download_dir: str = str(Path.home() / "Downloads" / "AIO Media Tool")
    image_dir: str = str(Path.home() / "Pictures" / "AIO Media Tool")
    video_dir: str = str(Path.home() / "Videos" / "AIO Media Tool")
    pdf_dir: str = str(Path.home() / "Documents" / "AIO Media Tool")
    transcription_dir: str = str(Path.home() / "Downloads" / "AIO Media Tool" / "Transkripte")
    privacy_dir: str = str(Path.home() / "Documents" / "AIO Media Tool" / "Clean")
    vault_dir: str = str(Path.home() / "Documents" / "AIO Media Tool" / "Vault")
    ocr_dir: str = str(Path.home() / "Documents" / "AIO Media Tool" / "OCR")
    upscale_dir: str = str(Path.home() / "Videos" / "AIO Media Tool" / "Upscaled")
    update_mode: str = "off"
    update_remote: str = "origin/main"
    update_interval_hours: int = 24
    last_update_check: str = ""
    detailed_logs: bool = False
    show_rights_notice: bool = True
    board_zoom: int = 100
    clipboard_enabled: bool = False
    clipboard_retention_hours: int = 24
    clipboard_blacklist: str = "1Password,Bitwarden,KeePass,LastPass,Proton Pass"
    tesseract_path: str = ""
    realesrgan_path: str = ""
    rife_path: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AppSettings:
        known = {key: value for key, value in raw.items() if key in cls.__dataclass_fields__}
        return cls(**known)

    def ensure_output_dirs(self) -> None:
        for value in (
            self.download_dir,
            self.image_dir,
            self.video_dir,
            self.pdf_dir,
            self.transcription_dir,
            self.privacy_dir,
            self.vault_dir,
            self.ocr_dir,
            self.upscale_dir,
        ):
            Path(value).expanduser().mkdir(parents=True, exist_ok=True)
