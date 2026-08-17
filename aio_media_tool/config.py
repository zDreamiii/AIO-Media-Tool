from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path

from aio_media_tool.models import AppSettings


class SettingsStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> AppSettings:
        if not self.path.exists():
            settings = AppSettings()
            settings.ensure_output_dirs()
            return settings
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            settings = AppSettings.from_dict(raw)
        except (OSError, ValueError, TypeError):
            settings = AppSettings()
        settings.ensure_output_dirs()
        return settings

    def save(self, settings: AppSettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(
            prefix="settings-", suffix=".json", dir=self.path.parent
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(asdict(settings), stream, indent=2, ensure_ascii=False)
            os.replace(temp_name, self.path)
        finally:
            Path(temp_name).unlink(missing_ok=True)
