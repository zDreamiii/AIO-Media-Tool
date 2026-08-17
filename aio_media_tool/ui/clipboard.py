from __future__ import annotations

from contextlib import suppress
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, QTimer, Signal
from PySide6.QtGui import QAction, QIcon, QImage
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from aio_media_tool.services.snippets import ActiveApplicationDetector, SnippetDatabase


class ClipboardManager(QObject):
    snippet_added = Signal()
    show_requested = Signal()
    quit_requested = Signal()

    def __init__(
        self, database: SnippetDatabase, icon_path: Path, parent: QObject | None = None
    ) -> None:
        super().__init__(parent)
        self.database = database
        self.clipboard = QApplication.clipboard()
        self.blacklist = ""
        self.retention_hours = 24
        self.enabled = False
        self.paused = False
        self._ignore_next = False
        self.tray = QSystemTrayIcon(QIcon(str(icon_path)), self)
        self.tray.setToolTip("AIO_M Smart Clipboard")
        menu = QMenu()
        show = QAction("AIO Media Tool öffnen", menu)
        show.triggered.connect(self.show_requested.emit)
        self.pause_action = QAction("Aufzeichnung pausieren", menu)
        self.pause_action.setCheckable(True)
        self.pause_action.toggled.connect(self.set_paused)
        clear = QAction("Verlauf leeren", menu)
        clear.triggered.connect(self._clear)
        quit_action = QAction("Beenden", menu)
        quit_action.triggered.connect(self.quit_requested.emit)
        menu.addAction(show)
        menu.addSeparator()
        menu.addAction(self.pause_action)
        menu.addAction(clear)
        menu.addSeparator()
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._activated)
        self.cleanup_timer = QTimer(self)
        self.cleanup_timer.setInterval(60 * 60 * 1000)
        self.cleanup_timer.timeout.connect(self.cleanup)

    def configure(self, enabled: bool, retention_hours: int, blacklist: str) -> None:
        self.blacklist = blacklist
        self.retention_hours = max(1, int(retention_hours))
        if enabled == self.enabled:
            self.cleanup()
            return
        self.enabled = enabled
        if enabled:
            self.clipboard.dataChanged.connect(self._capture)
            self.cleanup_timer.start()
            self.cleanup()
            if QSystemTrayIcon.isSystemTrayAvailable():
                self.tray.show()
        else:
            with suppress(RuntimeError):
                self.clipboard.dataChanged.disconnect(self._capture)
            self.cleanup_timer.stop()
            self.tray.hide()

    def set_paused(self, paused: bool) -> None:
        self.paused = paused
        self.pause_action.setText("Aufzeichnung fortsetzen" if paused else "Aufzeichnung pausieren")

    def cleanup(self) -> None:
        self.database.delete_older_than(self.retention_hours)
        self.snippet_added.emit()

    def copy_text(self, text: str) -> None:
        self._ignore_next = True
        self.clipboard.setText(text)

    def copy_image(self, image: QImage) -> None:
        self._ignore_next = True
        self.clipboard.setImage(image)

    def _capture(self) -> None:
        if self._ignore_next:
            self._ignore_next = False
            return
        if not self.enabled or self.paused:
            return
        source_app = ActiveApplicationDetector.name()
        if ActiveApplicationDetector.is_blacklisted(source_app, self.blacklist):
            return
        mime = self.clipboard.mimeData()
        inserted = None
        if mime.hasImage():
            image = self.clipboard.image()
            if not image.isNull():
                data = QByteArray()
                buffer = QBuffer(data)
                if buffer.open(QIODevice.OpenModeFlag.WriteOnly) and image.save(buffer, "PNG"):
                    inserted = self.database.add_image(
                        bytes(data), f"Bild {image.width()} × {image.height()}", source_app
                    )
        elif mime.hasText():
            inserted = self.database.add_text(mime.text(), source_app)
        if inserted is not None:
            self.snippet_added.emit()

    def _clear(self) -> None:
        self.database.clear()
        self.snippet_added.emit()

    def _activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in {
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        }:
            self.show_requested.emit()
