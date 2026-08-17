from __future__ import annotations

import json
import re
import shutil
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from threading import Event

from PySide6.QtCore import QObject, QPoint, QRect, QRunnable, Qt, QThreadPool, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from aio_media_tool.config import SettingsStore
from aio_media_tool.jobs import JobManager
from aio_media_tool.models import AppSettings, JobKind, JobRecord, JobStatus
from aio_media_tool.paths import AppPaths
from aio_media_tool.services.metadata_cleaner import MetadataCleanerService
from aio_media_tool.services.ocr import OCRResult, OCRService
from aio_media_tool.services.snippets import SnippetDatabase
from aio_media_tool.services.transcription import TranscriptionOptions, TranscriptionService
from aio_media_tool.services.upscaler import UpscaleOptions, UpscalerService
from aio_media_tool.services.vault import VaultService
from aio_media_tool.ui.clipboard import ClipboardManager
from aio_media_tool.ui.pages import BasePage, primary_button, show_validation
from aio_media_tool.ui.widgets import Card, FileDropList, PathPicker, muted, section_title


class TaskSignals(QObject):
    completed = Signal(object)
    failed = Signal(str)
    progress = Signal(int, str)


class TaskWorker(QRunnable):
    def __init__(self, function: Callable) -> None:
        super().__init__()
        self.function = function
        self.cancel = Event()
        self.signals = TaskSignals()

    @Slot()
    def run(self) -> None:
        try:
            self.signals.completed.emit(self.function(self.signals.progress.emit, self.cancel))
        except Exception as exc:
            self.signals.failed.emit(str(exc).strip() or type(exc).__name__)


def _start_worker(owner, worker: TaskWorker, completed: Callable, failed: Callable) -> None:
    owner._worker = worker
    worker.signals.completed.connect(completed)
    worker.signals.failed.connect(failed)
    QThreadPool.globalInstance().start(worker)


class TranscriptionPage(BasePage):
    def __init__(
        self,
        settings: AppSettings,
        jobs: JobManager,
        app_paths: AppPaths,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(
            "AIO_M Transkription",
            "Lokale Speech-to-Text-Erkennung mit faster-whisper, SRT/VTT-Export und optionalen FFmpeg-Hardsubs.",
            parent,
        )
        self.settings, self.jobs, self.app_paths = settings, jobs, app_paths
        available, note = TranscriptionService.available()
        status = QLabel(("● " if available else "○ ") + note)
        status.setObjectName("Badge")
        self.body.addWidget(status)
        card = Card()
        self.files = FileDropList(
            "Audio/Video (*.mp3 *.wav *.flac *.m4a *.aac *.ogg *.mp4 *.mkv *.mov *.webm *.avi)",
            {
                ".mp3",
                ".wav",
                ".flac",
                ".m4a",
                ".aac",
                ".ogg",
                ".mp4",
                ".mkv",
                ".mov",
                ".webm",
                ".avi",
            },
        )
        card.layout.addWidget(self.files)
        form = QFormLayout()
        self.model = QComboBox()
        for label, value in (
            ("Tiny (schnell)", "tiny"),
            ("Base", "base"),
            ("Small", "small"),
            ("Large v3 (genau)", "large"),
        ):
            self.model.addItem(label, value)
        self.model.setCurrentIndex(2)
        self.language = QComboBox()
        for label, value in (
            ("Auto-Detect", ""),
            ("Deutsch", "de"),
            ("Englisch", "en"),
            ("Französisch", "fr"),
            ("Spanisch", "es"),
            ("Italienisch", "it"),
            ("Japanisch", "ja"),
        ):
            self.language.addItem(label, value)
        self.device = QComboBox()
        self.device.addItem("Automatisch", "auto")
        self.device.addItem("CPU", "cpu")
        self.device.addItem("CUDA", "cuda")
        self.output = PathPicker(settings.transcription_dir)
        form.addRow("Modell:", self.model)
        form.addRow("Sprache:", self.language)
        form.addRow("Hardware:", self.device)
        form.addRow("Ausgabeordner:", self.output)
        card.layout.addLayout(form)
        flags = QHBoxLayout()
        self.srt = QCheckBox("SRT")
        self.srt.setChecked(True)
        self.vtt = QCheckBox("VTT")
        self.vtt.setChecked(True)
        self.hardsubs = QCheckBox("Untertitel als Hardsubs in Video einbrennen")
        self.offline = QCheckBox("Nur vorhandene Modelle (offline)")
        self.offline.setChecked(True)
        for widget in (self.srt, self.vtt, self.hardsubs, self.offline):
            flags.addWidget(widget)
        flags.addStretch()
        card.layout.addLayout(flags)
        card.layout.addWidget(
            muted(
                "Die Erkennung läuft lokal. Beim ersten Einsatz kann das Modell nach ausdrücklichem Abschalten des Offline-Hakens einmalig geladen werden."
            )
        )
        button = primary_button("Transkription starten")
        button.clicked.connect(self.submit)
        card.layout.addWidget(button)
        self.body.addWidget(card)
        self.finish()

    def submit(self) -> None:
        sources = self.files.paths()
        if not sources:
            show_validation(self, "Bitte mindestens eine Audio- oder Videodatei ablegen.")
            return
        output = Path(self.output.text()).expanduser()
        if not self.srt.isChecked() and not self.vtt.isChecked() and not self.hardsubs.isChecked():
            show_validation(self, "Bitte SRT, VTT oder Hardsubs auswählen.")
            return
        for source in sources:
            options = TranscriptionOptions(
                model=str(self.model.currentData()),
                language=str(self.language.currentData()),
                device=str(self.device.currentData()),
                offline_only=self.offline.isChecked(),
                write_srt=self.srt.isChecked(),
                write_vtt=self.vtt.isChecked(),
                burn_hardsubs=self.hardsubs.isChecked(),
            )

            def runner(progress, cancel, item=source, config=options):
                return TranscriptionService().transcribe(
                    item, output, self.app_paths.models / "whisper", config, progress, cancel
                )

            self.jobs.submit(
                JobKind.TRANSCRIPTION,
                f"Whisper: {source.name}",
                str(source),
                str(output),
                {
                    "model": options.model,
                    "language": options.language or "auto",
                    "hardsubs": options.burn_hardsubs,
                },
                runner,
            )


class PrivacyStripperPage(BasePage):
    def __init__(
        self, settings: AppSettings, jobs: JobManager, parent: QWidget | None = None
    ) -> None:
        super().__init__(
            "AIO_M Stripper · Deep Clean",
            "Entfernt eingebettete EXIF-, GPS-, Autoren-, Zeit-, Software- und Medien-Tags rekursiv und dokumentiert jede Änderung.",
            parent,
        )
        self.settings, self.jobs = settings, jobs
        self._worker: TaskWorker | None = None
        self._job_id = ""
        card = Card()
        form = QFormLayout()
        self.folder = PathPicker("", mode="directory")
        self.mode = QComboBox()
        self.mode.addItem("Auf bereinigten Kopien arbeiten", "copy")
        self.mode.addItem("Originale nach .bak verschieben", "backup")
        self.output = PathPicker(settings.privacy_dir, mode="directory")
        self.exiftool = PathPicker(shutil.which("exiftool") or "", mode="file")
        form.addRow("Quellordner:", self.folder)
        form.addRow("Sicherheitsmodus:", self.mode)
        form.addRow("Kopien-Ausgabe:", self.output)
        form.addRow("ExifTool (optional):", self.exiftool)
        card.layout.addLayout(form)
        actions = QHBoxLayout()
        scan = QPushButton("Ordner scannen")
        scan.clicked.connect(self.scan)
        clean = primary_button("Batch bereinigen")
        clean.clicked.connect(self.clean)
        actions.addWidget(scan)
        actions.addWidget(clean)
        actions.addStretch()
        card.layout.addLayout(actions)
        self.body.addWidget(card)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["Datei", "Typ", "Vorher", "Entfernt", "Nachher", "Ausgabe"]
        )
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setMinimumHeight(300)
        self.body.addWidget(self.table)
        self.jobs.job_updated.connect(self._job_updated)
        self.finish()

    def scan(self) -> None:
        folder = Path(self.folder.text()).expanduser()
        if not folder.is_dir():
            show_validation(self, "Bitte einen vorhandenen Quellordner auswählen.")
            return
        service = MetadataCleanerService(self.exiftool.text())
        worker = TaskWorker(
            lambda progress, cancel: [
                {"path": str(path), "metadata": service.read_metadata(path)}
                for path in service.scan(folder)
            ]
        )
        _start_worker(self, worker, self._scan_ready, self._failed)

    def _scan_ready(self, rows: list[dict]) -> None:
        self._worker = None
        self.table.setRowCount(len(rows))
        for row, item in enumerate(rows):
            path = Path(item["path"])
            metadata = item["metadata"]
            values = (str(path), path.suffix.upper().lstrip("."), str(len(metadata)), "–", "–", "–")
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)
                if column == 2:
                    cell.setToolTip(json.dumps(metadata, ensure_ascii=False, indent=2))
                self.table.setItem(row, column, cell)

    def clean(self) -> None:
        folder = Path(self.folder.text()).expanduser()
        if not folder.is_dir():
            show_validation(self, "Bitte einen vorhandenen Quellordner auswählen.")
            return
        mode = str(self.mode.currentData())
        output = Path(self.output.text()).expanduser() if mode == "copy" else None
        exiftool = self.exiftool.text()

        def runner(progress, cancel):
            return MetadataCleanerService(exiftool).clean_batch(
                folder, mode, output, progress, cancel
            )

        job = self.jobs.submit(
            JobKind.PRIVACY,
            f"Deep Clean: {folder.name}",
            str(folder),
            str(output or folder / ".bak"),
            {"mode": mode, "recursive": True},
            runner,
        )
        self._job_id = job.id

    def _job_updated(self, job: JobRecord) -> None:
        if job.id != self._job_id:
            return
        if job.status == JobStatus.COMPLETED:
            log = next((Path(path) for path in job.outputs if path.endswith(".json")), None)
            if log and log.is_file():
                with suppress(OSError, KeyError, json.JSONDecodeError):
                    self._show_clean_results(json.loads(log.read_text(encoding="utf-8"))["results"])
        elif job.status == JobStatus.FAILED:
            QMessageBox.warning(self, "Deep Clean fehlgeschlagen", job.error)

    def _show_clean_results(self, results: list[dict]) -> None:
        self.table.setRowCount(len(results))
        for row, result in enumerate(results):
            before, removed, after = result["before"], result["removed"], result["after"]
            values = (
                result["source"],
                Path(result["source"]).suffix.upper().lstrip("."),
                str(len(before)),
                str(len(removed)),
                str(len(after)),
                result["output"],
            )
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)
                details = (
                    before
                    if column == 2
                    else removed
                    if column == 3
                    else after
                    if column == 4
                    else None
                )
                if details is not None:
                    cell.setToolTip(json.dumps(details, ensure_ascii=False, indent=2))
                self.table.setItem(row, column, cell)

    def _failed(self, error: str) -> None:
        self._worker = None
        QMessageBox.warning(self, "Ordnerscan fehlgeschlagen", error)


class VaultPage(BasePage):
    def __init__(
        self,
        settings: AppSettings,
        jobs: JobManager,
        app_paths: AppPaths,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(
            "AIO_M Vault",
            "Dateien mit AES-256-GCM und PBKDF2 in einem authentifizierten .aio_enc-Archiv schützen oder sicher wiederherstellen.",
            parent,
        )
        self.settings, self.jobs, self.app_paths = settings, jobs, app_paths
        self._job_ids: set[str] = set()
        tabs = QTabWidget()
        tabs.addTab(self._encrypt_tab(), "Verschlüsseln")
        tabs.addTab(self._decrypt_tab(), "Entschlüsseln")
        self.body.addWidget(tabs)
        self.body.addWidget(
            muted(
                "Passwörter werden weder gespeichert noch in den Job-Verlauf geschrieben. Ohne Passwort kann ein Vault nicht wiederhergestellt werden."
            )
        )
        self.jobs.job_updated.connect(self._job_updated)
        self.finish()

    def _encrypt_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.vault_files = FileDropList("Alle Dateien (*)", set())
        self.vault_folder = PathPicker("", mode="directory")
        self.vault_output = PathPicker(
            str(Path(self.settings.vault_dir) / "Mein_Vault.aio_enc"),
            mode="save",
            file_filter="AIO_M Vault (*.aio_enc)",
        )
        self.vault_password = QLineEdit()
        self.vault_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.vault_confirm = QLineEdit()
        self.vault_confirm.setEchoMode(QLineEdit.EchoMode.Password)
        form = QFormLayout()
        form.addRow("Zusätzlicher Ordner:", self.vault_folder)
        form.addRow("Vault-Datei:", self.vault_output)
        form.addRow("Starkes Passwort:", self.vault_password)
        form.addRow("Passwort wiederholen:", self.vault_confirm)
        button = primary_button("Verschlüsselten Vault erstellen")
        button.clicked.connect(self.encrypt)
        layout.addWidget(self.vault_files)
        layout.addLayout(form)
        layout.addWidget(button)
        return page

    def _decrypt_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        form = QFormLayout()
        self.archive = PathPicker("", mode="file", file_filter="AIO_M Vault (*.aio_enc)")
        self.decrypt_output = PathPicker(self.settings.vault_dir, mode="directory")
        self.decrypt_password = QLineEdit()
        self.decrypt_password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Vault-Datei:", self.archive)
        form.addRow("Zielordner:", self.decrypt_output)
        form.addRow("Passwort:", self.decrypt_password)
        button = primary_button("Vault entschlüsseln")
        button.clicked.connect(self.decrypt)
        layout.addLayout(form)
        layout.addWidget(button)
        layout.addStretch()
        return page

    def encrypt(self) -> None:
        sources = self.vault_files.paths()
        folder = Path(self.vault_folder.text()).expanduser() if self.vault_folder.text() else None
        if folder and folder.is_dir():
            sources.append(folder)
        if not sources:
            show_validation(self, "Bitte Dateien ablegen oder einen Ordner auswählen.")
            return
        password = self.vault_password.text()
        if password != self.vault_confirm.text():
            show_validation(self, "Die beiden Passwörter stimmen nicht überein.")
            return
        try:
            VaultService.validate_password(password)
        except ValueError as exc:
            show_validation(self, str(exc))
            return
        output = Path(self.vault_output.text()).expanduser()

        def runner(progress, cancel):
            return VaultService().encrypt(sources, output, password, progress, cancel)

        job = self.jobs.submit(
            JobKind.VAULT,
            f"Vault erstellen: {output.name}",
            f"{len(sources)} Auswahl(en)",
            str(output),
            {"cipher": "AES-256-GCM", "kdf": "PBKDF2-SHA256", "items": len(sources)},
            runner,
        )
        self._job_ids.add(job.id)
        self.vault_password.clear()
        self.vault_confirm.clear()

    def decrypt(self) -> None:
        archive = Path(self.archive.text()).expanduser()
        output = Path(self.decrypt_output.text()).expanduser()
        password = self.decrypt_password.text()
        if not archive.is_file() or archive.suffix.casefold() != ".aio_enc":
            show_validation(self, "Bitte eine vorhandene .aio_enc-Datei auswählen.")
            return
        if not password:
            show_validation(self, "Bitte das Vault-Passwort eingeben.")
            return

        def runner(progress, cancel):
            return VaultService().decrypt(
                archive, output, password, self.app_paths.temp / "vault", progress, cancel
            )

        job = self.jobs.submit(
            JobKind.VAULT,
            f"Vault öffnen: {archive.name}",
            str(archive),
            str(output),
            {"operation": "decrypt"},
            runner,
        )
        self._job_ids.add(job.id)
        self.decrypt_password.clear()

    def _job_updated(self, job: JobRecord) -> None:
        if job.id not in self._job_ids:
            return
        if job.status == JobStatus.FAILED:
            QMessageBox.warning(self, "Vault konnte nicht geöffnet werden", job.error)
        if job.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}:
            self._job_ids.discard(job.id)


class ClipboardPage(BasePage):
    def __init__(
        self,
        settings: AppSettings,
        store: SettingsStore,
        database: SnippetDatabase,
        manager: ClipboardManager,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(
            "AIO_M Smart Clipboard",
            "Durchsuchbarer lokaler Verlauf für die letzten Texte und Bilder, mit Makros, Aufbewahrungsfrist und App-Blacklist.",
            parent,
        )
        self.settings, self.store, self.database, self.manager = settings, store, database, manager
        controls = Card()
        top = QHBoxLayout()
        self.enabled = QCheckBox("Clipboard-Monitoring und Tray-Icon aktiv")
        self.enabled.setChecked(settings.clipboard_enabled)
        self.retention = QSpinBox()
        self.retention.setRange(1, 720)
        self.retention.setValue(settings.clipboard_retention_hours)
        self.retention.setSuffix(" h")
        save = primary_button("Privacy-Einstellungen speichern")
        save.clicked.connect(self.save_settings)
        top.addWidget(self.enabled)
        top.addWidget(QLabel("Auto-Löschen nach"))
        top.addWidget(self.retention)
        top.addStretch()
        top.addWidget(save)
        self.blacklist = QLineEdit(settings.clipboard_blacklist)
        controls.layout.addLayout(top)
        controls.layout.addWidget(QLabel("Blacklist (App-Namen, mit Komma getrennt):"))
        controls.layout.addWidget(self.blacklist)
        controls.layout.addWidget(
            muted(
                "Die Quell-App lässt sich unter Wayland und auf manchen Desktops technisch nicht sicher erkennen. Für Passwörter Monitoring pausieren oder deaktivieren."
            )
        )
        self.body.addWidget(controls)
        self.search = QLineEdit()
        self.search.setPlaceholderText("In den letzten 100 Snippets suchen …")
        self.search.textChanged.connect(self.reload)
        self.body.addWidget(self.search)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Zeit", "Typ", "Inhalt", "Quell-App"])
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self.show_selected)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        split.addWidget(self.table)
        split.addWidget(self.preview)
        split.setSizes([700, 420])
        self.body.addWidget(split)
        actions = QHBoxLayout()
        for text, slot in (
            ("Als Plain Text kopieren", self.copy_plain),
            ("Whitespace trimmen", self.copy_trimmed),
            ("URLs extrahieren", self.copy_urls),
            ("Auswahl löschen", self.delete_selected),
            ("Alles löschen", self.clear_all),
        ):
            button = QPushButton(text)
            button.clicked.connect(slot)
            actions.addWidget(button)
        actions.addStretch()
        self.body.addLayout(actions)
        self.manager.snippet_added.connect(self.reload)
        self.reload()
        self.finish()

    def save_settings(self) -> None:
        self.settings.clipboard_enabled = self.enabled.isChecked()
        self.settings.clipboard_retention_hours = self.retention.value()
        self.settings.clipboard_blacklist = self.blacklist.text().strip()
        self.store.save(self.settings)
        self.manager.configure(
            self.settings.clipboard_enabled,
            self.settings.clipboard_retention_hours,
            self.settings.clipboard_blacklist,
        )

    def reload(self) -> None:
        snippets = self.database.recent(100, self.search.text())
        self.table.setRowCount(len(snippets))
        for row, snippet in enumerate(snippets):
            try:
                stamp = (
                    datetime.fromisoformat(snippet.created_at).astimezone().strftime("%d.%m. %H:%M")
                )
            except ValueError:
                stamp = snippet.created_at[:16]
            values = (
                stamp,
                "Bild" if snippet.kind == "image" else "Text",
                snippet.preview,
                snippet.source_app or "–",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, snippet.id)
                self.table.setItem(row, column, item)

    def _selected(self):
        row = self.table.currentRow()
        if row < 0 or not self.table.item(row, 0):
            return None
        return self.database.get(int(self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)))

    def show_selected(self) -> None:
        snippet = self._selected()
        if not snippet:
            self.preview.clear()
        elif snippet.kind == "text":
            self.preview.setPlainText(snippet.text_content)
        else:
            self.preview.setPlainText(
                f"{snippet.preview}\n\nPNG-Bild · {len(snippet.binary_content or b'')} Bytes"
            )

    def copy_plain(self) -> None:
        snippet = self._selected()
        if not snippet:
            return
        if snippet.kind == "image" and snippet.binary_content:
            self.manager.copy_image(QImage.fromData(snippet.binary_content, "PNG"))
        else:
            self.manager.copy_text(snippet.text_content)

    def copy_trimmed(self) -> None:
        snippet = self._selected()
        if snippet and snippet.kind == "text":
            self.manager.copy_text(
                "\n".join(line.strip() for line in snippet.text_content.strip().splitlines())
            )

    def copy_urls(self) -> None:
        snippet = self._selected()
        if snippet and snippet.kind == "text":
            urls = re.findall(r"https?://[^\s<>\]\[\"']+", snippet.text_content)
            self.manager.copy_text("\n".join(dict.fromkeys(urls)))

    def delete_selected(self) -> None:
        snippet = self._selected()
        if snippet:
            self.database.delete(snippet.id)
            self.reload()

    def clear_all(self) -> None:
        if (
            QMessageBox.question(
                self, "Clipboard-Verlauf löschen", "Alle lokalen Snippets unwiderruflich löschen?"
            )
            == QMessageBox.StandardButton.Yes
        ):
            self.database.clear()
            self.reload()


class OCRPage(BasePage):
    def __init__(
        self,
        settings: AppSettings,
        store: SettingsStore,
        app_paths: AppPaths,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(
            "AIO_M OCR & PDF-Übersetzer",
            "Erkennt Text spaltenweise aus Bildern oder PDFs und stellt Original und optionale Übersetzung direkt zur Bearbeitung gegenüber.",
            parent,
        )
        self.settings, self.store, self.app_paths = settings, store, app_paths
        self._worker: TaskWorker | None = None
        card = Card()
        form = QFormLayout()
        self.source = PathPicker(
            "",
            mode="file",
            file_filter="OCR-Dateien (*.pdf *.jpg *.jpeg *.png *.tif *.tiff *.bmp *.webp)",
        )
        self.engine = QComboBox()
        self.engine.addItem("Automatisch", "auto")
        self.engine.addItem("Tesseract", "tesseract")
        self.engine.addItem("EasyOCR", "easyocr")
        self.languages = QLineEdit("deu+eng")
        self.tesseract = PathPicker(
            settings.tesseract_path or shutil.which("tesseract") or "", mode="file"
        )
        form.addRow("PDF/Bild:", self.source)
        form.addRow("OCR-Engine:", self.engine)
        form.addRow("Sprachen:", self.languages)
        form.addRow("Tesseract-Binary:", self.tesseract)
        card.layout.addLayout(form)
        recognize = primary_button("Text erkennen")
        recognize.clicked.connect(self.recognize)
        card.layout.addWidget(recognize)
        self.ocr_status = muted("Bereit")
        card.layout.addWidget(self.ocr_status)
        self.body.addWidget(card)
        editors = QSplitter(Qt.Orientation.Horizontal)
        self.original = QPlainTextEdit()
        self.original.setPlaceholderText("Erkannter Originaltext")
        self.translation = QPlainTextEdit()
        self.translation.setPlaceholderText("Optionale Übersetzung")
        left, right = QWidget(), QWidget()
        left_layout, right_layout = QVBoxLayout(left), QVBoxLayout(right)
        left_layout.addWidget(section_title("Original"))
        left_layout.addWidget(self.original)
        right_layout.addWidget(section_title("Übersetzung"))
        right_layout.addWidget(self.translation)
        editors.addWidget(left)
        editors.addWidget(right)
        editors.setSizes([600, 600])
        self.body.addWidget(editors)
        translate_card = Card()
        translate_form = QFormLayout()
        self.translator = QComboBox()
        self.translator.addItem("DeepL API Free", "deepl_free")
        self.translator.addItem("DeepL API Pro", "deepl_pro")
        self.translator.addItem("MarianMT lokal", "marian")
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.target_language = QLineEdit("DE")
        self.marian_model = PathPicker("", mode="directory")
        translate_form.addRow("Übersetzer:", self.translator)
        translate_form.addRow("DeepL-Key (nicht gespeichert):", self.api_key)
        translate_form.addRow("Zielsprache:", self.target_language)
        translate_form.addRow("Lokaler MarianMT-Ordner:", self.marian_model)
        translate_card.layout.addLayout(translate_form)
        translate_card.layout.addWidget(
            muted(
                "Nur DeepL sendet den Editorinhalt an einen externen Dienst; MarianMT bleibt vollständig lokal."
            )
        )
        translate = QPushButton("Übersetzen")
        translate.clicked.connect(self.translate)
        export_row = QHBoxLayout()
        export_txt = QPushButton("Als TXT exportieren")
        export_txt.clicked.connect(lambda: self.export("txt"))
        export_docx = QPushButton("Als DOCX exportieren")
        export_docx.clicked.connect(lambda: self.export("docx"))
        export_row.addWidget(translate)
        export_row.addStretch()
        export_row.addWidget(export_txt)
        export_row.addWidget(export_docx)
        translate_card.layout.addLayout(export_row)
        self.body.addWidget(translate_card)
        self.finish()

    def recognize(self) -> None:
        source = Path(self.source.text()).expanduser()
        if not source.is_file():
            show_validation(self, "Bitte eine vorhandene PDF- oder Bilddatei auswählen.")
            return
        tesseract = self.tesseract.text()
        self.settings.tesseract_path = tesseract
        self.store.save(self.settings)

        def task(progress, cancel):
            return OCRService().recognize(
                source,
                str(self.engine.currentData()),
                self.languages.text().strip(),
                tesseract,
                self.app_paths.temp / "ocr",
                progress,
                cancel,
            )

        worker = TaskWorker(task)
        worker.signals.progress.connect(
            lambda value, message: self.ocr_status.setText(f"{value}% · {message}")
        )
        _start_worker(self, worker, self._ocr_ready, self._failed)

    def _ocr_ready(self, result: OCRResult) -> None:
        self._worker = None
        self.original.setPlainText(result.text)
        self.ocr_status.setText(f"Fertig · {len(result.pages)} Seite(n) · {result.engine}")

    def translate(self) -> None:
        text = self.original.toPlainText()
        if not text.strip():
            show_validation(self, "Zuerst Text erkennen oder in den Original-Editor einfügen.")
            return
        mode = str(self.translator.currentData())
        key = self.api_key.text()
        target = self.target_language.text().strip() or "DE"

        def task(progress, cancel):
            if mode == "marian":
                return OCRService.translate_marian(
                    text, Path(self.marian_model.text()), progress, cancel
                )
            return OCRService.translate_deepl(
                text, key, target, mode == "deepl_free", progress, cancel
            )

        worker = TaskWorker(task)
        worker.signals.progress.connect(
            lambda value, message: self.ocr_status.setText(f"{value}% · {message}")
        )
        _start_worker(self, worker, self._translation_ready, self._failed)
        self.api_key.clear()

    def _translation_ready(self, text: str) -> None:
        self._worker = None
        self.translation.setPlainText(text)
        self.ocr_status.setText("Übersetzung fertig")

    def export(self, kind: str) -> None:
        default = str(Path(self.settings.ocr_dir) / f"ocr-export.{kind}")
        path, _ = QFileDialog.getSaveFileName(
            self, "OCR exportieren", default, f"{kind.upper()} (*.{kind})"
        )
        if not path:
            return
        try:
            if kind == "docx":
                output = OCRService.export_docx(
                    self.original.toPlainText(), self.translation.toPlainText(), Path(path)
                )
            else:
                output = OCRService.export_txt(
                    self.original.toPlainText(), self.translation.toPlainText(), Path(path)
                )
        except Exception as exc:
            self._failed(str(exc))
            return
        QMessageBox.information(self, "Export fertig", str(output))

    def _failed(self, error: str) -> None:
        self._worker = None
        self.ocr_status.setText("Fehlgeschlagen")
        QMessageBox.warning(self, "OCR/Übersetzung fehlgeschlagen", error)


class BeforeAfterSlider(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.before = QPixmap()
        self.after = QPixmap()
        self.position = 0.5
        self.setMinimumHeight(320)
        self.setMouseTracking(True)

    def set_images(self, before: Path, after: Path) -> None:
        self.before = QPixmap(str(before))
        self.after = QPixmap(str(after))
        self.position = 0.5
        self.update()

    def _image_rect(self) -> QRect:
        if self.before.isNull():
            return self.rect().adjusted(12, 12, -12, -12)
        size = self.before.size()
        size.scale(
            self.rect().adjusted(12, 12, -12, -12).size(), Qt.AspectRatioMode.KeepAspectRatio
        )
        return QRect(
            QPoint((self.width() - size.width()) // 2, (self.height() - size.height()) // 2), size
        )

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#10141c"))
        if self.before.isNull() or self.after.isNull():
            painter.setPen(QColor("#8992a5"))
            painter.drawText(
                self.rect(),
                Qt.AlignmentFlag.AlignCenter,
                "1-Sekunden-Vorschau noch nicht gerendert",
            )
            return
        target = self._image_rect()
        painter.drawPixmap(target, self.before)
        divider = target.left() + int(target.width() * self.position)
        painter.save()
        painter.setClipRect(
            QRect(divider, target.top(), target.right() - divider + 1, target.height())
        )
        painter.drawPixmap(target, self.after)
        painter.restore()
        painter.setPen(QPen(QColor("#8a7dff"), 3))
        painter.drawLine(divider, target.top(), divider, target.bottom())
        painter.setBrush(QColor("#705cf6"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QPoint(divider, target.center().y()), 9, 9)
        painter.setPen(QColor("white"))
        painter.drawText(
            target.adjusted(12, 8, -12, -8),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
            "VORHER",
        )
        painter.drawText(
            target.adjusted(12, 8, -12, -8),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop,
            "NACHHER",
        )

    def _move(self, event: QMouseEvent) -> None:
        target = self._image_rect()
        self.position = min(
            1.0, max(0.0, (event.position().x() - target.left()) / max(1, target.width()))
        )
        self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._move(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if event.buttons() & Qt.MouseButton.LeftButton:
            self._move(event)


class UpscalerPage(BasePage):
    def __init__(
        self,
        settings: AppSettings,
        store: SettingsStore,
        jobs: JobManager,
        app_paths: AppPaths,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(
            "AIO_M KI-Upscaler",
            "Real-ESRGAN für Bilder/Videos und RIFE für Framerate-Interpolation – mit Queue, ETA und interaktivem Vorher/Nachher-Slider.",
            parent,
        )
        self.settings, self.store, self.jobs, self.app_paths = settings, store, jobs, app_paths
        self._worker: TaskWorker | None = None
        self.hardware = QLabel("Hardware wird geprüft …")
        self.hardware.setObjectName("Badge")
        self.hardware.setWordWrap(True)
        self.body.addWidget(self.hardware)
        card = Card()
        self.source = FileDropList(
            "Medien (*.jpg *.jpeg *.png *.webp *.mp4 *.mkv *.mov *.webm *.avi)",
            IMAGE_EXTENSIONS | VIDEO_EXTENSIONS,
            multiple=False,
        )
        card.layout.addWidget(self.source)
        form = QFormLayout()
        self.realesrgan = PathPicker(
            settings.realesrgan_path or shutil.which("realesrgan-ncnn-vulkan") or "", mode="file"
        )
        self.rife = PathPicker(
            settings.rife_path or shutil.which("rife-ncnn-vulkan") or "", mode="file"
        )
        self.scale = QComboBox()
        for label, value in (("Nur Interpolation (1×)", 1), ("2×", 2), ("3×", 3), ("4×", 4)):
            self.scale.addItem(label, value)
        self.scale.setCurrentIndex(1)
        self.model = QComboBox()
        self.model.addItems(
            [
                "realesrgan-x4plus",
                "realesrgan-x4plus-anime",
                "realesr-animevideov3",
                "realesrnet-x4plus",
            ]
        )
        self.interpolate = QCheckBox("RIFE-Framerate-Interpolation aktivieren")
        self.fps = QSpinBox()
        self.fps.setRange(24, 240)
        self.fps.setValue(60)
        self.tile = QSpinBox()
        self.tile.setRange(0, 2048)
        self.tile.setSpecialValueText("Auto")
        self.output = PathPicker(settings.upscale_dir, mode="directory")
        form.addRow("Real-ESRGAN-Binary:", self.realesrgan)
        form.addRow("RIFE-Binary:", self.rife)
        form.addRow("Skalierung:", self.scale)
        form.addRow("Modell:", self.model)
        form.addRow("Interpolation:", self.interpolate)
        form.addRow("Ziel-FPS:", self.fps)
        form.addRow("Tile-Größe:", self.tile)
        form.addRow("Ausgabeordner:", self.output)
        card.layout.addLayout(form)
        actions = QHBoxLayout()
        preview = QPushButton("1-Sekunden-Vorschau rendern")
        preview.clicked.connect(self.render_preview)
        start = primary_button("In Queue starten")
        start.clicked.connect(self.submit)
        actions.addWidget(preview)
        actions.addWidget(start)
        actions.addStretch()
        card.layout.addLayout(actions)
        self.upscale_status = muted(
            "Portable NCNN-Binaries enthalten ihre Modelle; es werden keine Medien hochgeladen."
        )
        card.layout.addWidget(self.upscale_status)
        self.body.addWidget(card)
        self.slider = BeforeAfterSlider()
        self.body.addWidget(self.slider)
        self.finish()
        QTimer.singleShot(0, self.check_hardware)

    def check_hardware(self) -> None:
        worker = TaskWorker(lambda _progress, _cancel: UpscalerService.detect_hardware())
        _start_worker(self, worker, self._hardware_ready, self._failed)

    def _hardware_ready(self, report) -> None:
        self._worker = None
        self.hardware.setText(
            f"CUDA: {report.cuda} · Vulkan: {report.vulkan} · DirectML: {report.directml} · Empfehlung: {report.recommendation}"
        )

    def _options(self) -> UpscaleOptions:
        return UpscaleOptions(
            scale=int(self.scale.currentData()),
            model=self.model.currentText(),
            interpolate=self.interpolate.isChecked(),
            target_fps=self.fps.value(),
            tile_size=self.tile.value(),
        )

    def _validate(self) -> Path | None:
        sources = self.source.paths()
        if not sources:
            show_validation(self, "Bitte ein Bild oder Video ablegen.")
            return None
        if int(self.scale.currentData()) == 1 and not self.interpolate.isChecked():
            show_validation(self, "Bei 1× bitte RIFE-Interpolation aktivieren.")
            return None
        return sources[0]

    def submit(self) -> None:
        source = self._validate()
        if not source:
            return
        options = self._options()
        output = Path(self.output.text()).expanduser()
        self._save_binary_paths()

        def runner(progress, cancel):
            return UpscalerService().process(
                source,
                output,
                self.app_paths.temp / "upscaler",
                options,
                self.realesrgan.text(),
                self.rife.text(),
                progress,
                cancel,
            )

        self.jobs.submit(
            JobKind.UPSCALE,
            f"KI-Upscale: {source.name}",
            str(source),
            str(output),
            {
                "scale": options.scale,
                "model": options.model,
                "target_fps": options.target_fps if options.interpolate else 0,
            },
            runner,
        )

    def render_preview(self) -> None:
        source = self._validate()
        if not source:
            return
        options = self._options()
        self._save_binary_paths()

        def task(progress, cancel):
            return UpscalerService().create_preview(
                source,
                self.app_paths.temp / "previews",
                self.app_paths.temp / "upscaler",
                options,
                self.realesrgan.text(),
                self.rife.text(),
                progress,
                cancel,
            )

        worker = TaskWorker(task)
        worker.signals.progress.connect(
            lambda value, message: self.upscale_status.setText(f"{value}% · {message}")
        )
        _start_worker(self, worker, self._preview_ready, self._failed)

    def _preview_ready(self, paths: tuple[Path, Path]) -> None:
        self._worker = None
        self.slider.set_images(*paths)
        self.upscale_status.setText("Vorschau fertig – Trennlinie mit der Maus ziehen")

    def _save_binary_paths(self) -> None:
        self.settings.realesrgan_path = self.realesrgan.text()
        self.settings.rife_path = self.rife.text()
        self.store.save(self.settings)

    def _failed(self, error: str) -> None:
        self._worker = None
        self.upscale_status.setText("Fehlgeschlagen")
        QMessageBox.warning(self, "KI-Upscaler", error)


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v"}
