from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

APP_SLUG = "aio-media-tool"


def _data_root() -> Path:
    override = os.environ.get("AIO_MEDIA_TOOL_HOME")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        return (
            Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
            / "AIO Media Tool"
        )
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "AIO Media Tool"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / APP_SLUG


@dataclass(frozen=True, slots=True)
class AppPaths:
    data: Path
    config: Path
    database: Path
    logs: Path
    temp: Path
    workspace: Path
    snippets: Path
    models: Path

    @classmethod
    def create(cls) -> AppPaths:
        data = _data_root()
        result = cls(
            data=data,
            config=data / "settings.json",
            database=data / "history.sqlite3",
            logs=data / "logs",
            temp=data / "temp",
            workspace=data / "workspace.json",
            snippets=data / "AIO_M_Snippets.sqlite3",
            models=data / "models",
        )
        result.data.mkdir(parents=True, exist_ok=True)
        result.logs.mkdir(parents=True, exist_ok=True)
        result.temp.mkdir(parents=True, exist_ok=True)
        result.models.mkdir(parents=True, exist_ok=True)
        return result


def project_root() -> Path:
    override = os.environ.get("AIO_MEDIA_TOOL_PROJECT_ROOT")
    if override:
        candidate = Path(override).expanduser().resolve()
        if (candidate / "pyproject.toml").exists():
            return candidate
    if getattr(sys, "frozen", False):
        marker = Path(sys.executable).resolve().parent / "source-root.txt"
        if marker.is_file():
            try:
                candidate = Path(marker.read_text(encoding="utf-8").strip()).resolve()
                if (candidate / "pyproject.toml").exists():
                    return candidate
            except OSError:
                pass
    current = Path(__file__).resolve().parent
    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").exists():
            return candidate
    return current.parent
