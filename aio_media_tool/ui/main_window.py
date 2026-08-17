from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QCloseEvent, QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from aio_media_tool import __version__
from aio_media_tool.config import SettingsStore
from aio_media_tool.database import HistoryDatabase
from aio_media_tool.jobs import JobManager
from aio_media_tool.models import AppSettings, JobRecord, JobStatus
from aio_media_tool.paths import AppPaths
from aio_media_tool.runtime import is_frozen
from aio_media_tool.services.snippets import SnippetDatabase
from aio_media_tool.services.updater import UpdateReport, UpdaterService
from aio_media_tool.services.workspace import WorkspaceStore
from aio_media_tool.ui.ai_security_pages import (
    ClipboardPage,
    OCRPage,
    PrivacyStripperPage,
    TranscriptionPage,
    UpscalerPage,
    VaultPage,
)
from aio_media_tool.ui.board import BoardPage
from aio_media_tool.ui.clipboard import ClipboardManager
from aio_media_tool.ui.files_page import BulkRenamerPage
from aio_media_tool.ui.pages import (
    BasePage,
    DashboardPage,
    DownloadPage,
    HistoryPage,
    ImagesPage,
    MusicPage,
    PdfPage,
    QueuePage,
    SettingsPage,
    VideosPage,
    primary_button,
)
from aio_media_tool.ui.theme import DARK_THEME, LIGHT_THEME
from aio_media_tool.ui.widgets import Card, muted, section_title


class UpdateSignals(QObject):
    completed = Signal(object)
    failed = Signal(str)


class UpdateWorker(QRunnable):
    def __init__(self, remote: str, include_packages: bool) -> None:
        super().__init__()
        self.remote = remote
        self.include_packages = include_packages
        self.signals = UpdateSignals()

    @Slot()
    def run(self) -> None:
        try:
            self.signals.completed.emit(UpdaterService().check(self.remote, self.include_packages))
        except Exception as exc:
            self.signals.failed.emit(str(exc))


class AIActivationPage(BasePage):
    """Session-only gate that keeps optional AI pages cold until a user opts in."""

    activation_requested = Signal()

    def __init__(
        self,
        title: str,
        module_name: str,
        resource_note: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(
            title,
            "Optionales KI-Modul im Ruhemodus – es wird in dieser Sitzung erst nach deinem Klick geladen.",
            parent,
        )
        self.status = QLabel("○ KI AUS · KEINE HINTERGRUNDLAST")
        self.status.setObjectName("Badge")
        self.body.addWidget(self.status)
        card = Card()
        card.layout.addWidget(section_title(f"{module_name} für diese Sitzung aktivieren"))
        card.layout.addWidget(
            muted(
                "Bis zur Aktivierung werden weder Modelle importiert oder geladen noch GPU-/Hardware-Checks oder KI-Subprozesse gestartet."
            )
        )
        card.layout.addWidget(muted(resource_note))
        card.layout.addWidget(
            muted(
                "Die Freigabe gilt nur bis zum Beenden der App. Beim nächsten Start ist das Modul wieder aus."
            )
        )
        self.activate_button = primary_button("KI-Modul jetzt aktivieren")
        self.activate_button.clicked.connect(self._request_activation)
        card.layout.addWidget(self.activate_button)
        self.body.addWidget(card)
        self.finish()

    def _request_activation(self) -> None:
        self.activate_button.setEnabled(False)
        self.activate_button.setText("KI-Modul wird geöffnet …")
        self.status.setText("Aktivierung durch Nutzer angefordert …")
        self.activation_requested.emit()

    def set_failed(self, error: str) -> None:
        self.status.setText(f"Aktivierung fehlgeschlagen · {error}")
        self.activate_button.setText("Erneut versuchen")
        self.activate_button.setEnabled(True)


class MainWindow(QMainWindow):
    NAVIGATION = (
        ("START", "Übersicht", 0),
        ("MEDIEN", "Download", 1),
        ("MEDIEN", "Musik", 2),
        ("MEDIEN", "Bilder", 3),
        ("MEDIEN", "Videos", 4),
        ("MEDIEN", "Transkription", 11),
        ("MEDIEN", "KI-Upscaler", 16),
        ("DOKUMENTE", "PDFs", 5),
        ("DOKUMENTE", "OCR & Übersetzen", 15),
        ("DATEIEN & PRIVACY", "Bulk-Renamer", 6),
        ("DATEIEN & PRIVACY", "Metadaten-Stripper", 12),
        ("DATEIEN & PRIVACY", "Vault", 13),
        ("WISSEN", "Sammlungen", 7),
        ("WISSEN", "Smart Clipboard", 14),
        ("SYSTEM", "Queue", 8),
        ("SYSTEM", "Verlauf", 9),
        ("SYSTEM", "Einstellungen", 10),
    )

    def __init__(self, app_paths: AppPaths, store: SettingsStore, settings: AppSettings) -> None:
        super().__init__()
        self.app_paths = app_paths
        self.store = store
        self.settings = settings
        self.database = HistoryDatabase(app_paths.database)
        self.jobs = JobManager(self.database, settings.parallel_jobs, self)
        self.snippet_database = SnippetDatabase(app_paths.snippets)
        icon_name = "app_icon.ico" if sys.platform == "win32" else "app_icon.svg"
        icon = Path(__file__).resolve().parents[1] / "resources" / icon_name
        self.clipboard_manager = ClipboardManager(self.snippet_database, icon, self)
        self._force_quit = False
        self._tray_hint_shown = False
        self._update_worker: UpdateWorker | None = None
        self.setWindowTitle(f"AIO Media Tool {__version__}")
        self.setMinimumSize(1100, 720)
        self.resize(1320, 840)
        self._build_ui()
        self.apply_theme(settings.theme)
        self._connect()
        self.clipboard_manager.configure(
            settings.clipboard_enabled,
            settings.clipboard_retention_hours,
            settings.clipboard_blacklist,
        )
        QTimer.singleShot(750, self._show_rights_notice)
        QTimer.singleShot(1600, self._automatic_update_check)

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("Root")
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(218)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(15, 19, 15, 17)
        side.setSpacing(6)
        brand_row = QHBoxLayout()
        mark = QLabel("A")
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark.setFixedSize(38, 38)
        mark.setStyleSheet(
            "background:#705cf6;border-radius:11px;color:white;font-size:18px;font-weight:800;"
        )
        brand = QVBoxLayout()
        brand.setSpacing(0)
        brand_name = QLabel("AIO MEDIA")
        brand_name.setObjectName("Brand")
        brand_note = QLabel("DESKTOP TOOLKIT")
        brand_note.setObjectName("BrandAccent")
        brand.addWidget(brand_name)
        brand.addWidget(brand_note)
        brand_row.addWidget(mark)
        brand_row.addLayout(brand)
        brand_row.addStretch()
        side.addLayout(brand_row)
        side.addSpacing(12)
        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.nav_buttons: dict[int, QPushButton] = {}
        nav_scroll = QScrollArea()
        nav_scroll.setWidgetResizable(True)
        nav_scroll.setFrameShape(QFrame.Shape.NoFrame)
        nav_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        nav_scroll.setStyleSheet(
            "QScrollArea, QScrollArea > QWidget, QScrollArea > QWidget > QWidget "
            "{ background: transparent; border: 0; }"
        )
        nav_scroll.viewport().setAutoFillBackground(False)
        nav_content = QWidget()
        nav_content.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        nav_layout = QVBoxLayout(nav_content)
        nav_layout.setContentsMargins(0, 0, 3, 0)
        nav_layout.setSpacing(4)
        previous_group = ""
        for group, text, index in self.NAVIGATION:
            if group != previous_group:
                if previous_group:
                    nav_layout.addSpacing(7)
                group_label = QLabel(group)
                group_label.setObjectName("BrandAccent")
                nav_layout.addWidget(group_label)
                previous_group = group
            button = QPushButton(text)
            button.setObjectName("Nav")
            button.setCheckable(True)
            button.clicked.connect(lambda _checked=False, page=index: self.set_page(page))
            self.nav_group.addButton(button, index)
            self.nav_buttons[index] = button
            nav_layout.addWidget(button)
        nav_layout.addStretch()
        nav_scroll.setWidget(nav_content)
        side.addWidget(nav_scroll, 1)
        privacy = QLabel("LOKAL · KEINE TELEMETRIE")
        privacy.setObjectName("BrandAccent")
        privacy.setAlignment(Qt.AlignmentFlag.AlignCenter)
        side.addWidget(privacy)
        version = QLabel(f"Alpha {__version__}")
        version.setObjectName("Muted")
        version.setAlignment(Qt.AlignmentFlag.AlignCenter)
        side.addWidget(version)
        root_layout.addWidget(sidebar)

        workspace = QWidget()
        workspace.setObjectName("Root")
        workspace_layout = QVBoxLayout(workspace)
        workspace_layout.setContentsMargins(0, 0, 0, 0)
        workspace_layout.setSpacing(0)
        topbar = QFrame()
        topbar.setObjectName("Topbar")
        topbar_layout = QHBoxLayout(topbar)
        topbar_layout.setContentsMargins(24, 10, 24, 10)
        self.context_label = QLabel("Bereit")
        self.context_label.setObjectName("Muted")
        self.update_badge = QLabel("Updateprüfung ausstehend")
        self.update_badge.setObjectName("Badge")
        open_downloads = QPushButton("Ausgabe öffnen")
        open_downloads.clicked.connect(self.open_default_output)
        topbar_layout.addWidget(self.context_label)
        topbar_layout.addStretch()
        topbar_layout.addWidget(self.update_badge)
        topbar_layout.addWidget(open_downloads)
        workspace_layout.addWidget(topbar)
        self.stack = QStackedWidget()
        self.dashboard = DashboardPage(self.database)
        self.downloads = DownloadPage(self.settings, self.jobs)
        self.music = MusicPage(self.settings, self.jobs)
        self.images = ImagesPage(self.settings, self.jobs)
        self.videos = VideosPage(self.settings, self.jobs)
        self.pdfs = PdfPage(self.settings, self.jobs)
        self.files = BulkRenamerPage(self.jobs)
        self.board = BoardPage(
            self.settings,
            self.store,
            WorkspaceStore(self.app_paths.workspace),
            self.jobs,
        )
        self.queue = QueuePage(self.jobs)
        self.history = HistoryPage(self.database)
        self.settings_page = SettingsPage(self.settings, self.store, self.app_paths)
        self.transcription: TranscriptionPage | None = None
        self.stripper = PrivacyStripperPage(self.settings, self.jobs)
        self.vault = VaultPage(self.settings, self.jobs, self.app_paths)
        self.clipboard_page = ClipboardPage(
            self.settings,
            self.store,
            self.snippet_database,
            self.clipboard_manager,
        )
        self.ocr: OCRPage | None = None
        self.upscaler: UpscalerPage | None = None
        self._ai_placeholders = {
            11: AIActivationPage(
                "AIO_M Transkription",
                "Whisper-Transkription",
                "Nach der Aktivierung prüft die Seite nur die Verfügbarkeit. Das Whisper-Modell wird erst mit „Transkription starten“ geladen.",
            ),
            15: AIActivationPage(
                "AIO_M OCR & PDF-Übersetzer",
                "OCR- und Übersetzungswerkzeuge",
                "Tesseract, EasyOCR und MarianMT werden nicht vorab gestartet. Auch ein lokales Übersetzungsmodell lädt erst beim eigentlichen Auftrag.",
            ),
            16: AIActivationPage(
                "AIO_M KI-Upscaler",
                "Upscaling und Video-Interpolation",
                "Erst nach der Aktivierung läuft der Hardware-Check. Real-ESRGAN und RIFE starten ausschließlich über Vorschau oder Queue-Auftrag.",
            ),
        }
        for index, placeholder in self._ai_placeholders.items():
            placeholder.activation_requested.connect(
                lambda page_index=index: self._activate_ai_page(page_index)
            )
        for page in (
            self.dashboard,
            self.downloads,
            self.music,
            self.images,
            self.videos,
            self.pdfs,
            self.files,
            self.board,
            self.queue,
            self.history,
            self.settings_page,
            self._ai_placeholders[11],
            self.stripper,
            self.vault,
            self.clipboard_page,
            self._ai_placeholders[15],
            self._ai_placeholders[16],
        ):
            self.stack.addWidget(page)
        workspace_layout.addWidget(self.stack, 1)
        root_layout.addWidget(workspace, 1)
        self.setCentralWidget(root)
        self.set_page(0)

    def _connect(self) -> None:
        self.dashboard.navigate.connect(self.set_page)
        self.jobs.activity_changed.connect(self._activity_changed)
        self.jobs.job_updated.connect(self._job_updated)
        self.settings_page.settings_saved.connect(self._settings_saved)
        self.settings_page.update_check_requested.connect(self.check_updates)
        self.clipboard_manager.show_requested.connect(self._show_from_tray)
        self.clipboard_manager.quit_requested.connect(self._quit_from_tray)

    def set_page(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        if index in self.nav_buttons:
            self.nav_buttons[index].setChecked(True)
        if index == 0:
            self.dashboard.refresh()
        elif index == 9:
            self.history.reload()

    def _activate_ai_page(self, index: int) -> None:
        placeholder = self._ai_placeholders.get(index)
        if placeholder is None:
            self.set_page(index)
            return
        try:
            if index == 11:
                page = TranscriptionPage(self.settings, self.jobs, self.app_paths)
                self.transcription = page
            elif index == 15:
                page = OCRPage(self.settings, self.store, self.app_paths)
                self.ocr = page
            elif index == 16:
                page = UpscalerPage(
                    self.settings,
                    self.store,
                    self.jobs,
                    self.app_paths,
                )
                self.upscaler = page
            else:
                raise ValueError(f"Unbekannte KI-Seite: {index}")
        except Exception as exc:
            placeholder.set_failed(str(exc).strip() or type(exc).__name__)
            return
        self.stack.removeWidget(placeholder)
        self.stack.insertWidget(index, page)
        self._ai_placeholders.pop(index, None)
        placeholder.deleteLater()
        self.context_label.setText("KI-Modul für diese Sitzung aktiviert")
        self.set_page(index)

    def apply_theme(self, theme: str) -> None:
        app = QApplication.instance()
        if app:
            app.setStyleSheet(LIGHT_THEME if theme == "light" else DARK_THEME)

    def _settings_saved(self, settings: AppSettings) -> None:
        self.settings = settings
        self.jobs.set_max_workers(settings.parallel_jobs)
        self.apply_theme(settings.theme)
        self.downloads.output.setText(settings.download_dir)
        self.music.music_output.setText(settings.download_dir)
        self.music.local_output.setText(settings.download_dir)
        self.images.image_output.setText(settings.image_dir)
        self.videos.video_output.setText(settings.video_dir)
        self.videos.gif_output.setText(settings.video_dir)
        self.pdfs.pdf_output.setText(settings.pdf_dir)
        if self.transcription is not None:
            self.transcription.output.setText(settings.transcription_dir)
        self.stripper.output.setText(settings.privacy_dir)
        self.vault.vault_output.setText(str(Path(settings.vault_dir) / "Mein_Vault.aio_enc"))
        self.vault.decrypt_output.setText(settings.vault_dir)
        if self.ocr is not None:
            self.ocr.settings = settings
        if self.upscaler is not None:
            self.upscaler.output.setText(settings.upscale_dir)

    def _activity_changed(self, count: int) -> None:
        self.context_label.setText("Bereit" if count == 0 else f"{count} Aufgabe(n) aktiv")
        self.nav_buttons[8].setText("Queue" if count == 0 else f"Queue  ·  {count}")

    def _job_updated(self, job: JobRecord) -> None:
        if job.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}:
            self.history.reload()
            self.dashboard.refresh()

    def open_default_output(self) -> None:
        path = Path(self.settings.download_dir).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _show_rights_notice(self) -> None:
        if not self.settings.show_rights_notice:
            return
        box = QMessageBox(self)
        box.setWindowTitle("Willkommen")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText("AIO Media Tool verarbeitet Medien lokal.")
        box.setInformativeText(
            "Downloads sind nur für Inhalte vorgesehen, die dir gehören oder die du rechtmäßig speichern darfst. Schutzmaßnahmen, Logins und Bezahlschranken werden nicht umgangen."
        )
        again = QCheckBox("Diesen Hinweis beim Start erneut anzeigen")
        again.setChecked(False)
        box.setCheckBox(again)
        box.exec()
        self.settings.show_rights_notice = again.isChecked()
        self.store.save(self.settings)

    def _automatic_update_check(self) -> None:
        if is_frozen():
            self.update_badge.setText("Release-Version")
            self.settings_page.set_update_status("EXE-Version: Updates über neue GitHub-Releases")
            return
        if UpdaterService.is_due(self.settings):
            self.check_updates(False)
        elif self.settings.update_mode == "off":
            self.update_badge.setText("Updates aus")
            self.settings_page.set_update_status("Updates sind deaktiviert")
        else:
            self.update_badge.setText("Kürzlich geprüft")

    def check_updates(self, force: bool = False) -> None:
        if is_frozen():
            self.update_badge.setText("Release-Version")
            self.settings_page.set_update_status("EXE-Version: Updates über neue GitHub-Releases")
            return
        if self._update_worker is not None:
            return
        if not force and not UpdaterService.is_due(self.settings):
            return
        self.update_badge.setText("Prüft Updates …")
        self.settings_page.set_update_status("Prüfung läuft …")
        include_packages = self.settings.update_mode == "code_and_packages"
        worker = UpdateWorker(self.settings.update_remote, include_packages)
        self._update_worker = worker
        worker.signals.completed.connect(self._update_checked)
        worker.signals.failed.connect(self._update_failed)
        QThreadPool.globalInstance().start(worker)

    def _update_checked(self, report: UpdateReport) -> None:
        self._update_worker = None
        self.settings.last_update_check = datetime.now(UTC).isoformat()
        self.store.save(self.settings)
        self.update_badge.setText(report.summary)
        self.settings_page.set_update_status(report.summary)
        if report.notes:
            self.settings_page.set_update_status(f"{report.summary} · {report.notes[0]}")
        if not report.update_available or not report.clean_repository:
            return
        if self.settings.update_mode in {"code", "code_and_packages"}:
            self._apply_update()
            return
        answer = QMessageBox.question(
            self,
            "Update verfügbar",
            f"{report.summary}. Jetzt aktualisieren, testen und die App neu bauen?",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._apply_update()

    def _update_failed(self, error: str) -> None:
        self._update_worker = None
        self.update_badge.setText("Updateprüfung fehlgeschlagen")
        self.settings_page.set_update_status(f"Fehlgeschlagen: {error}")

    def _apply_update(self) -> None:
        if self.jobs.active_count():
            self.settings_page.set_update_status("Update wartet, solange Aufgaben aktiv sind")
            return
        try:
            UpdaterService().launch_update(
                self.settings.update_remote,
                self.settings.update_mode == "code_and_packages",
            )
        except Exception as exc:
            QMessageBox.critical(self, "Update konnte nicht starten", str(exc))
            return
        QMessageBox.information(
            self,
            "Update wird installiert",
            "Die App wird beendet. Der separate Updater aktualisiert, testet, baut neu und startet die App wieder.",
        )
        QTimer.singleShot(250, QApplication.instance().quit)

    def closeEvent(self, event: QCloseEvent) -> None:
        if (
            self.settings.clipboard_enabled
            and not self._force_quit
            and self.clipboard_manager.tray.isVisible()
        ):
            event.ignore()
            self.hide()
            if not self._tray_hint_shown:
                self.clipboard_manager.tray.showMessage(
                    "AIO_M Smart Clipboard",
                    "Die App läuft lokal im Tray weiter. Über das Tray-Menü kann sie beendet werden.",
                )
                self._tray_hint_shown = True
            return
        active = self.jobs.active_count()
        if active:
            answer = QMessageBox.question(
                self,
                "Aktive Aufgaben",
                f"{active} Aufgabe(n) laufen noch. Wirklich abbrechen und beenden?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.jobs.cancel_all()
            self.jobs.pool.waitForDone(2500)
        event.accept()

    def _show_from_tray(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def _quit_from_tray(self) -> None:
        self._force_quit = True
        self.close()
