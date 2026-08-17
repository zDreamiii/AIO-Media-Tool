from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QGuiApplication, QIcon
from PySide6.QtWidgets import QApplication

from aio_media_tool.config import SettingsStore
from aio_media_tool.paths import AppPaths
from aio_media_tool.ui.main_window import MainWindow


def configure_logging(paths: AppPaths, detailed: bool) -> None:
    handler = RotatingFileHandler(
        paths.logs / "application.log", maxBytes=1_500_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if detailed else logging.INFO)
    root.addHandler(handler)


def main() -> int:
    paths = AppPaths.create()
    store = SettingsStore(paths.config)
    settings = store.load()
    configure_logging(paths, settings.detailed_logs)
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setApplicationName("AIO Media Tool")
    app.setOrganizationName("AIO Media Tool")
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 10))
    resource_dir = Path(__file__).resolve().parent / "resources"
    icon_name = "app_icon.ico" if sys.platform == "win32" else "app_icon.svg"
    app.setWindowIcon(QIcon(str(resource_dir / icon_name)))
    window = MainWindow(paths, store, settings)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
