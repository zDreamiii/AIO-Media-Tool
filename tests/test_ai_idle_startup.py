from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
except ImportError as exc:  # pragma: no cover - depends on Linux GUI runtime packages
    pytest.skip(f"Qt-Laufzeit für UI-Test nicht verfügbar: {exc}", allow_module_level=True)

from aio_media_tool.config import SettingsStore
from aio_media_tool.models import AppSettings
from aio_media_tool.paths import AppPaths
from aio_media_tool.services.transcription import TranscriptionService
from aio_media_tool.services.upscaler import UpscalerService
from aio_media_tool.ui.ai_security_pages import TranscriptionPage
from aio_media_tool.ui.main_window import AIActivationPage, MainWindow


def _paths(root: Path) -> AppPaths:
    return AppPaths(
        data=root,
        config=root / "settings.json",
        database=root / "history.sqlite3",
        logs=root / "logs",
        temp=root / "temp",
        workspace=root / "workspace.json",
        snippets=root / "snippets.sqlite3",
        models=root / "models",
    )


def _settings(root: Path) -> AppSettings:
    settings = AppSettings(show_rights_notice=False, update_mode="off")
    for name in (
        "download_dir",
        "image_dir",
        "video_dir",
        "pdf_dir",
        "transcription_dir",
        "privacy_dir",
        "vault_dir",
        "ocr_dir",
        "upscale_dir",
    ):
        setattr(settings, name, str(root / name))
    return settings


def test_ai_pages_stay_cold_until_activation(tmp_path: Path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    paths = _paths(tmp_path)
    settings = _settings(tmp_path)
    calls = {"whisper": 0, "hardware": 0}

    def whisper_probe() -> tuple[bool, str]:
        calls["whisper"] += 1
        return True, "faster-whisper ist installiert und noch nicht geladen"

    def hardware_probe():
        calls["hardware"] += 1
        raise AssertionError("Hardware-Scan darf beim App-Start nicht laufen")

    monkeypatch.setattr(TranscriptionService, "available", staticmethod(whisper_probe))
    monkeypatch.setattr(UpscalerService, "detect_hardware", staticmethod(hardware_probe))

    window = MainWindow(paths, SettingsStore(paths.config), settings)
    try:
        assert calls == {"whisper": 0, "hardware": 0}
        assert window.transcription is None
        assert window.ocr is None
        assert window.upscaler is None
        assert all(
            isinstance(window.stack.widget(index), AIActivationPage) for index in (11, 15, 16)
        )

        gate = window.stack.widget(11)
        assert isinstance(gate, AIActivationPage)
        gate.activate_button.click()

        assert calls == {"whisper": 1, "hardware": 0}
        assert isinstance(window.transcription, TranscriptionPage)
        assert window.stack.widget(11) is window.transcription
    finally:
        window.close()
        app.processEvents()
