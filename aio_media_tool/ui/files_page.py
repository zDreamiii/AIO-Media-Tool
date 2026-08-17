from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from aio_media_tool.jobs import JobManager
from aio_media_tool.models import JobKind, JobStatus
from aio_media_tool.services.files import BulkRenameService, RenameOptions, RenamePreview
from aio_media_tool.ui.widgets import Card, PageHeader, PathPicker, muted, section_title


class BulkRenamerPage(QScrollArea):
    def __init__(self, jobs: JobManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.jobs = jobs
        self._rows: list[RenamePreview] = []
        self._active_job = ""
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        content.setObjectName("Root")
        body = QVBoxLayout(content)
        body.setContentsMargins(30, 26, 30, 50)
        body.setSpacing(18)
        body.addWidget(
            PageHeader(
                "Bulk-Renamer",
                "Ordner ablegen, hunderte Dateinamen vorab prüfen und in einem sicheren Zwei-Phasen-Schritt umbenennen.",
            )
        )
        settings = Card()
        settings.layout.addWidget(section_title("Ordner und Muster"))
        form = QFormLayout()
        self.folder = PathPicker(mode="directory")
        self.folder.edit.setPlaceholderText("Ordner hier ablegen oder auswählen")
        self.template = QLineEdit("{date}_{name}_{n}")
        self.template.setPlaceholderText("z. B. {date}_{name}_{n}")
        self.extensions = QLineEdit()
        self.extensions.setPlaceholderText("leer = alle Dateien, sonst z. B. jpg png mp4")
        self.date_source = QComboBox()
        self.date_source.addItem("Änderungsdatum", "modified")
        self.date_source.addItem("Erstellungsdatum (Systemwert)", "created")
        self.date_source.addItem("Heutiges Datum", "now")
        self.date_format = QLineEdit("%Y-%m-%d")
        form.addRow("Ordner:", self.folder)
        form.addRow("Namensmuster:", self.template)
        form.addRow("Dateiendungen:", self.extensions)
        form.addRow("Datum aus:", self.date_source)
        form.addRow("Datumsformat:", self.date_format)
        settings.layout.addLayout(form)
        grid = QGridLayout()
        self.start = QSpinBox()
        self.start.setRange(0, 10_000_000)
        self.start.setValue(1)
        self.padding = QSpinBox()
        self.padding.setRange(1, 12)
        self.padding.setValue(3)
        self.regex = QLineEdit()
        self.regex.setPlaceholderText("optional, z. B. ^IMG_\\d+_")
        self.replacement = QLineEdit()
        self.replacement.setPlaceholderText("Regex-Ersatz; Rückgruppen wie \\1 möglich")
        grid.addWidget(QLabel("Startnummer:"), 0, 0)
        grid.addWidget(self.start, 0, 1)
        grid.addWidget(QLabel("Stellen:"), 0, 2)
        grid.addWidget(self.padding, 0, 3)
        grid.addWidget(QLabel("Regex auf Originalname:"), 1, 0)
        grid.addWidget(self.regex, 1, 1)
        grid.addWidget(QLabel("Ersetzen durch:"), 1, 2)
        grid.addWidget(self.replacement, 1, 3)
        settings.layout.addLayout(grid)
        self.recursive = QCheckBox("Unterordner einbeziehen (Dateien bleiben in ihrem Ordner)")
        settings.layout.addWidget(self.recursive)
        settings.layout.addWidget(
            muted(
                "Platzhalter: {name} Originalname · {ext} Endung · {date} Datum · {datetime} Datum/Uhrzeit · {n} Nummer · {parent} Ordner. Die Vorschau erkennt doppelte und bereits belegte Ziele."
            )
        )
        settings_actions = QHBoxLayout()
        settings_actions.addStretch()
        preview = QPushButton("Vorschau aktualisieren")
        preview.clicked.connect(self.refresh_preview)
        settings_actions.addWidget(preview)
        settings.layout.addLayout(settings_actions)
        body.addWidget(settings)

        preview_card = Card(padding=1)
        self.summary = QLabel(" Noch keine Vorschau")
        self.summary.setObjectName("Muted")
        preview_card.layout.addWidget(self.summary)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Bisher", "Neu", "Status"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setMinimumHeight(310)
        preview_card.layout.addWidget(self.table)
        apply_row = QHBoxLayout()
        apply_row.addStretch()
        self.apply_button = QPushButton("Umbenennen ausführen")
        self.apply_button.setObjectName("Primary")
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(self.apply)
        apply_row.addWidget(self.apply_button)
        preview_card.layout.addLayout(apply_row)
        body.addWidget(preview_card)
        body.addStretch()
        self.setWidget(content)
        self.jobs.job_updated.connect(self._job_updated)

    def _options(self) -> RenameOptions:
        return RenameOptions(
            template=self.template.text().strip(),
            start=self.start.value(),
            padding=self.padding.value(),
            date_format=self.date_format.text().strip() or "%Y-%m-%d",
            date_source=self.date_source.currentData(),
            regex_pattern=self.regex.text(),
            regex_replacement=self.replacement.text(),
            extensions=self.extensions.text(),
            recursive=self.recursive.isChecked(),
        )

    def refresh_preview(self) -> None:
        folder = Path(self.folder.text()).expanduser()
        try:
            self._rows = BulkRenameService().preview(folder, self._options())
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Vorschau nicht möglich", str(exc))
            return
        self.table.setRowCount(len(self._rows))
        errors = 0
        changes = 0
        for row, preview in enumerate(self._rows):
            relative_source = preview.source.relative_to(folder)
            relative_destination = preview.destination.relative_to(folder)
            status = preview.error or "Bereit"
            errors += bool(preview.error and preview.error != "Unverändert")
            changes += preview.source != preview.destination
            self.table.setItem(row, 0, QTableWidgetItem(str(relative_source)))
            self.table.setItem(row, 1, QTableWidgetItem(str(relative_destination)))
            self.table.setItem(row, 2, QTableWidgetItem(status))
        self.summary.setText(
            f" {len(self._rows)} Datei(en) · {changes} Änderung(en) · {errors} Konflikt(e)"
        )
        self.apply_button.setEnabled(changes > 0 and errors == 0 and not self._active_job)

    def apply(self) -> None:
        folder = Path(self.folder.text()).expanduser()
        options = self._options()
        changes = sum(row.source != row.destination for row in self._rows)
        if not changes:
            return
        answer = QMessageBox.question(
            self,
            "Dateien umbenennen",
            f"{changes} Datei(en) jetzt wie in der Vorschau umbenennen?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        def runner(progress, cancel):
            return BulkRenameService().apply(folder, options, progress, cancel)

        job = self.jobs.submit(
            JobKind.RENAME,
            f"{changes} Datei(en) umbenennen",
            str(folder),
            str(folder),
            {"template": options.template, "recursive": options.recursive},
            runner,
        )
        self._active_job = job.id
        self.apply_button.setEnabled(False)

    def _job_updated(self, job) -> None:
        if job.id != self._active_job:
            return
        if job.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}:
            self._active_job = ""
            self.refresh_preview()
